"""文件夹可见性：私有文件夹及其子树只对 admin 会话可见。"""
from typing import Optional, Set

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Folder


async def _folder_graph(db: AsyncSession, user_id: int):
    rows = (await db.execute(
        select(Folder.id, Folder.parent_id, Folder.is_private).where(Folder.user_id == user_id)
    )).all()
    children = {}
    private_roots = []
    for fid, pid, is_priv in rows:
        children.setdefault(pid, []).append(fid)
        if is_priv:
            private_roots.append(fid)
    return children, private_roots, {fid for fid, _p, _v in rows}


async def hidden_folder_ids(db: AsyncSession, user_id: int) -> Set[int]:
    """All folder ids that a viewer must not see (private folders + descendants)."""
    children, private_roots, _all = await _folder_graph(db, user_id)
    hidden: Set[int] = set()
    stack = list(private_roots)
    while stack:
        cur = stack.pop()
        if cur in hidden:
            continue
        hidden.add(cur)
        stack.extend(children.get(cur, []))
    return hidden


async def visible_folder_ids(db: AsyncSession, user_id: int, scope: str) -> Optional[Set[int]]:
    """Visible folder ids, or None when everything is visible (admin)."""
    if scope == "admin":
        return None
    children, private_roots, all_ids = await _folder_graph(db, user_id)
    hidden: Set[int] = set()
    stack = list(private_roots)
    while stack:
        cur = stack.pop()
        if cur in hidden:
            continue
        hidden.add(cur)
        stack.extend(children.get(cur, []))
    return all_ids - hidden


async def file_visible(db: AsyncSession, user_id: int, folder_id: Optional[int], scope: str) -> bool:
    """Is a file (given its folder) visible to this scope?"""
    if scope == "admin" or folder_id is None:
        return True
    hidden = await hidden_folder_ids(db, user_id)
    return folder_id not in hidden
