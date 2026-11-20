"""
Folder management API endpoints.
"""
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from ..database import get_db
from ..models import Folder, File, User
from ..schemas import FolderResponse, FolderCreate, FolderUpdate, FolderWithChildren
from ..auth import get_current_user, require_admin, get_scope
from ..visibility import hidden_folder_ids, visible_folder_ids
from ..telegram import delete_from_storage_channel


router = APIRouter(prefix="/folders", tags=["Folders"])


async def get_folder_file_count(db: AsyncSession, folder_id: int) -> int:
    """Get the total number of files in a folder and all its subfolders."""
    # This is a recursive CTE approach for efficiency
    from sqlalchemy import text
    query = text("""
        WITH RECURSIVE subfolders AS (
            SELECT id FROM folders WHERE id = :root_id
            UNION ALL
            SELECT f.id FROM folders f
            INNER JOIN subfolders sf ON f.parent_id = sf.id
        )
        SELECT COUNT(*) FROM files
        WHERE folder_id IN (SELECT id FROM subfolders)
    """)
    result = await db.execute(query, {"root_id": folder_id})
    return result.scalar() or 0


@router.get("", response_model=List[FolderResponse])
async def list_folders(
    parent_id: Optional[int] = Query(None, description="Filter by parent folder ID"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    scope: str = Depends(get_scope),
):
    """List user's folders optimally."""
    # Select folder and count of files in one query
    stmt = (
        select(Folder, func.count(File.id).label("file_count"))
        .outerjoin(File, File.folder_id == Folder.id)
        .where(Folder.user_id == current_user.id)
        .group_by(Folder.id)
        .order_by(Folder.name)
    )
    
    if parent_id is not None:
        if parent_id == 0:
            # 0 is the sentinel for "root level" so clients can request top-level only
            stmt = stmt.where(Folder.parent_id.is_(None))
        else:
            stmt = stmt.where(Folder.parent_id == parent_id)
    else:
        stmt = stmt.where(Folder.parent_id.is_(None))
    
    result = await db.execute(stmt)
    rows = result.all()

    if scope != "admin":
        hidden = await hidden_folder_ids(db, current_user.id)
        rows = [(f, c) for f, c in rows if f.id not in hidden]

    # 递归统计：子文件夹里的文件也算进父文件夹，避免「有子文件夹却显示 0 个文件」
    all_folders = await db.execute(
        select(Folder.id, Folder.parent_id).where(Folder.user_id == current_user.id)
    )
    children = {}
    for fid, pid in all_folders.all():
        children.setdefault(pid, []).append(fid)

    counts_res = await db.execute(
        select(File.folder_id, func.count(File.id))
        .join(Folder, Folder.id == File.folder_id)
        .where(Folder.user_id == current_user.id)
        .group_by(File.folder_id)
    )
    direct_counts = {fid: cnt for fid, cnt in counts_res.all() if fid is not None}
    memo = {}

    def subtree_count(fid):
        if fid in memo:
            return memo[fid]
        total = direct_counts.get(fid, 0)
        for child in children.get(fid, []):
            total += subtree_count(child)
        memo[fid] = total
        return total

    return [
        FolderResponse(
            id=folder.id,
            name=folder.name,
            parent_id=folder.parent_id,
            user_id=folder.user_id,
            created_at=folder.created_at,
            updated_at=folder.updated_at,
            file_count=subtree_count(folder.id),
        is_private=folder.is_private,
        )
        for folder, _direct in rows
    ]


@router.get("/tree", response_model=List[FolderWithChildren])
async def get_folder_tree(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    scope: str = Depends(get_scope),
):
    """Get the complete folder tree for the user optimally."""
    # Get all folders with counts in one query
    stmt = (
        select(Folder, func.count(File.id).label("file_count"))
        .outerjoin(File, File.folder_id == Folder.id)
        .where(Folder.user_id == current_user.id)
        .group_by(Folder.id)
        .order_by(Folder.name)
    )
    
    result = await db.execute(stmt)
    rows = result.all()

    if scope != "admin":
        hidden = await hidden_folder_ids(db, current_user.id)
        rows = [(f, c) for f, c in rows if f.id not in hidden]

    # Build tree
    folder_map = {}
    for folder, file_count in rows:
        folder_map[folder.id] = {
            "id": folder.id,
            "name": folder.name,
            "parent_id": folder.parent_id,
            "user_id": folder.user_id,
            "created_at": folder.created_at,
            "updated_at": folder.updated_at,
            "file_count": file_count,
            "is_private": folder.is_private,
            "children": [],
        }
    
    # Link parents and children
    roots = []
    for folder_data in folder_map.values():
        parent_id = folder_data["parent_id"]
        if parent_id and parent_id in folder_map:
            folder_map[parent_id]["children"].append(folder_data)
        else:
            roots.append(folder_data)
    
    return roots


@router.get("/{folder_id}/download-list")
async def folder_download_list(
    folder_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    scope: str = Depends(get_scope),
):
    """Return the subtree of a folder with relative paths, for building download lists."""
    res = await db.execute(
        select(Folder).where(Folder.id == folder_id, Folder.user_id == current_user.id)
    )
    root = res.scalar_one_or_none()
    if not root:
        raise HTTPException(status_code=404, detail="Folder not found")

    if scope != "admin":
        hidden = await hidden_folder_ids(db, current_user.id)
        if root.id in hidden:
            raise HTTPException(status_code=404, detail="Folder not found")

    folders = (await db.execute(
        select(Folder).where(Folder.user_id == current_user.id)
    )).scalars().all()
    by_id = {f.id: f for f in folders}
    children = {}
    for f in folders:
        children.setdefault(f.parent_id, []).append(f)

    hidden = await hidden_folder_ids(db, current_user.id) if scope != "admin" else set()

    # 收集子树（保持路径）
    entries = []
    stack = [(root, root.name)]
    while stack:
        folder, prefix = stack.pop()
        if folder.id in hidden:
            continue
        files = (await db.execute(
            select(File).where(File.folder_id == folder.id, File.user_id == current_user.id)
            .order_by(File.file_name)
        )).scalars().all()
        for f in files:
            entries.append({"id": f.id, "name": f.file_name,
                            "path": prefix + "/" + f.file_name, "size": f.file_size})
        for child in sorted(children.get(folder.id, []), key=lambda x: x.name):
            stack.append((child, prefix + "/" + child.name))

    entries.sort(key=lambda e: e["path"])
    from ..auth import create_download_token
    token_30d = create_download_token(current_user.telegram_id, current_user.auth_version, root.id, 30)
    return {
        "download_token": token_30d,
        "folder": {"id": root.id, "name": root.name},
        "count": len(entries),
        "total_bytes": sum(e["size"] for e in entries),
        "entries": entries,
    }

@router.get("/{folder_id}", response_model=FolderResponse)
async def get_folder(
    folder_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    scope: str = Depends(get_scope),
):
    """Get a specific folder by ID with file count."""
    stmt = (
        select(Folder, func.count(File.id).label("file_count"))
        .outerjoin(File, File.folder_id == Folder.id)
        .where(Folder.id == folder_id, Folder.user_id == current_user.id)
        .group_by(Folder.id)
    )
    
    result = await db.execute(stmt)
    row = result.first()
    
    if not row:
        raise HTTPException(status_code=404, detail="Folder not found")
    
    folder, file_count = row

    if scope != "admin":
        hidden = await hidden_folder_ids(db, current_user.id)
        if folder.id in hidden:
            raise HTTPException(status_code=404, detail="Folder not found")

    return FolderResponse(
        id=folder.id,
        name=folder.name,
        parent_id=folder.parent_id,
        user_id=folder.user_id,
        created_at=folder.created_at,
        updated_at=folder.updated_at,
        file_count=file_count,
        is_private=folder.is_private,
    )


@router.post("", response_model=FolderResponse, status_code=201)
async def create_folder(
    folder_data: FolderCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Create a new folder."""
    # Validate parent exists if specified
    if folder_data.parent_id:
        parent_result = await db.execute(
            select(Folder).where(
                Folder.id == folder_data.parent_id,
                Folder.user_id == current_user.id
            )
        )
        if not parent_result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Parent folder not found")
    
    # Check for duplicate name in same parent
    existing = await db.execute(
        select(Folder).where(
            Folder.user_id == current_user.id,
            Folder.parent_id == folder_data.parent_id,
            Folder.name == folder_data.name
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Folder with this name already exists")
    
    folder = Folder(
        user_id=current_user.id,
        name=folder_data.name,
        parent_id=folder_data.parent_id,
    )
    db.add(folder)
    await db.commit()
    await db.refresh(folder)
    
    return FolderResponse(
        id=folder.id,
        name=folder.name,
        parent_id=folder.parent_id,
        user_id=folder.user_id,
        created_at=folder.created_at,
        updated_at=folder.updated_at,
        file_count=0,
        is_private=folder.is_private,
    )


@router.patch("/{folder_id}", response_model=FolderResponse)
async def update_folder(
    folder_id: int,
    update_data: FolderUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Update a folder (rename, move)."""
    result = await db.execute(
        select(Folder).where(Folder.id == folder_id, Folder.user_id == current_user.id)
    )
    folder = result.scalar_one_or_none()
    
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    
    # Update fields
    if getattr(update_data, "is_private", None) is not None:
        folder.is_private = bool(update_data.is_private)

    if update_data.name is not None:
        folder.name = update_data.name
    if update_data.parent_id is not None:
        # Prevent moving folder into itself
        if update_data.parent_id == folder_id:
            raise HTTPException(status_code=400, detail="Cannot move folder into itself")
        
        # Verify target parent folder belongs to current user (security check)
        if update_data.parent_id != 0:
            parent_check = await db.execute(
                select(Folder).where(
                    Folder.id == update_data.parent_id,
                    Folder.user_id == current_user.id
                )
            )
            if not parent_check.scalar_one_or_none():
                raise HTTPException(status_code=404, detail="Parent folder not found")
        
        folder.parent_id = update_data.parent_id if update_data.parent_id != 0 else None
    
    await db.commit()
    await db.refresh(folder)
    
    file_count = await get_folder_file_count(db, folder.id)
    
    return FolderResponse(
        id=folder.id,
        name=folder.name,
        parent_id=folder.parent_id,
        user_id=folder.user_id,
        created_at=folder.created_at,
        updated_at=folder.updated_at,
        file_count=file_count,
        is_private=folder.is_private,
    )


@router.delete("/{folder_id}")
async def delete_folder(
    folder_id: int,
    move_files_to: Optional[int] = Query(None, description="Move files to this folder ID (null = root)"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Delete a folder. Files can be moved to another folder or deleted."""
    result = await db.execute(
        select(Folder).where(Folder.id == folder_id, Folder.user_id == current_user.id)
    )
    folder = result.scalar_one_or_none()
    
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    
    # Move files if specified
    if move_files_to is not None:
        from sqlalchemy import update
        await db.execute(
            update(File)
            .where(File.folder_id == folder_id)
            .values(folder_id=move_files_to if move_files_to != 0 else None)
        )
    else:
        # Recursive delete of files in this folder AND subfolders
        from sqlalchemy import text, delete
        
        # 1. Get all folder IDs (root + descendents)
        query = text("""
            WITH RECURSIVE subfolders AS (
                SELECT id FROM folders WHERE id = :root_id
                UNION ALL
                SELECT f.id FROM folders f
                INNER JOIN subfolders sf ON f.parent_id = sf.id
            )
            SELECT id FROM subfolders
        """)
        result = await db.execute(query, {"root_id": folder_id})
        folder_ids = result.scalars().all()
        
        if folder_ids:
            # 2. Get files to delete from Telegram
            file_query = select(File).where(File.folder_id.in_(folder_ids))
            file_result = await db.execute(file_query)
            files_to_delete = file_result.scalars().all()
            
            # Collect all message IDs for batch deletion
            message_ids = [f.channel_message_id for f in files_to_delete if f.channel_message_id]
            
            if message_ids:
                # Chunk into batches of 100 to avoid Telegram limits/errors
                chunk_size = 100
                for i in range(0, len(message_ids), chunk_size):
                    batch = message_ids[i:i + chunk_size]
                    await delete_from_storage_channel(batch)
            
            # 3. Delete files from DB
            await db.execute(delete(File).where(File.folder_id.in_(folder_ids)))
    
    # Delete folder (cascade will handle child folders)
    await db.delete(folder)
    await db.commit()
    
    return {"message": "Folder deleted successfully"}
@router.post("/batch-delete")
async def batch_delete_folders(
    folder_ids: List[int],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Delete multiple folders."""
    # Fetch all folders
    result = await db.execute(
        select(Folder).where(Folder.id.in_(folder_ids), Folder.user_id == current_user.id)
    )
    folders = result.scalars().all()
    
    if not folders:
        return {"message": "No folders found to delete"}
    
    # Recursive delete of files in all these folders
    from sqlalchemy import text, delete as sqlalchemy_delete
    
    all_affected_folder_ids = []
    for folder in folders:
        # Get all subfolder IDs for this folder
        query = text("""
            WITH RECURSIVE subfolders AS (
                SELECT id FROM folders WHERE id = :root_id
                UNION ALL
                SELECT f.id FROM folders f
                INNER JOIN subfolders sf ON f.parent_id = sf.id
            )
            SELECT id FROM subfolders
        """)
        folder_result = await db.execute(query, {"root_id": folder.id})
        all_affected_folder_ids.extend(folder_result.scalars().all())
    
    # Remove duplicates
    all_affected_folder_ids = list(set(all_affected_folder_ids))
    
    if all_affected_folder_ids:
        # Get files to delete from Telegram
        file_query = select(File).where(File.folder_id.in_(all_affected_folder_ids))
        file_result = await db.execute(file_query)
        files_to_delete = file_result.scalars().all()
        
        # Collect message IDs
        message_ids = [f.channel_message_id for f in files_to_delete if f.channel_message_id]
        
        if message_ids:
            # Batch delete from Telegram
            await delete_from_storage_channel(message_ids)
        
        # Delete files from DB
        await db.execute(sqlalchemy_delete(File).where(File.folder_id.in_(all_affected_folder_ids)))
    
    # Delete folders from DB
    for folder in folders:
        await db.delete(folder)
        
    await db.commit()
    
    return {"message": f"Deleted {len(folders)} folders and their content"}
@router.post("/batch-move")
async def batch_move_folders(
    move_data: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Move multiple folders to another folder."""
    folder_ids = move_data.get("ids", [])
    target_id = move_data.get("folder_id")
    
    if target_id == 0:
        target_id = None
        
    # Prevent moving a folder into itself or into one of its descendants
    if target_id in folder_ids:
        raise HTTPException(status_code=400, detail="Cannot move a folder into itself")

    if target_id is not None:
        ancestors = set()
        cursor = target_id
        while cursor is not None and cursor not in ancestors:
            if cursor in folder_ids:
                raise HTTPException(
                    status_code=400,
                    detail="Cannot move a folder into its own subfolder",
                )
            ancestors.add(cursor)
            row = await db.execute(
                select(Folder.parent_id).where(
                    Folder.id == cursor, Folder.user_id == current_user.id
                )
            )
            cursor = row.scalar_one_or_none()
        
    # Verify target parent folder belongs to user
    if target_id is not None:
        parent_check = await db.execute(
            select(Folder).where(Folder.id == target_id, Folder.user_id == current_user.id)
        )
        if not parent_check.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Target parent folder not found")
            
    # Update folders
    from sqlalchemy import update
    await db.execute(
        update(Folder)
        .where(Folder.id.in_(folder_ids), Folder.user_id == current_user.id)
        .values(parent_id=target_id)
    )
    
    await db.commit()
    return {"message": f"Moved {len(folder_ids)} folders"}
