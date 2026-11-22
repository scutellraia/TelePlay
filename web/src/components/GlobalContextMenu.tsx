import { useEffect, useRef, useState } from 'react';
import { useAppStore } from '../lib/store';
import { useQueryClient } from '@tanstack/react-query';
import { TelegramFile, Folder, api, authToken } from '../lib/api';
import { Play, Download, Link, Edit, FolderInput, Trash2, Globe, ShieldOff, HardDriveDownload, Lock } from 'lucide-react';
import { copyText } from '../lib/clipboard';

export default function GlobalContextMenu() {
    const { activeContextMenu, setActiveContextMenu, setPreviewFile, setMoveItems, setMoveFiles, setDeleteConfirm, setRenameFile, setRenameFolder, selectedFileIds, selectedFiles, addToast } = useAppStore();
    const menuRef = useRef<HTMLDivElement>(null);
    const [copiedId, setCopiedId] = useState<string | null>(null);

    const queryClient = useQueryClient();
    const buildDownloadList = async (folder: any) => {
        const t = authToken();
        if (!t) { alert('请先登录'); return; }
        try {
            const { data } = await api.get(`/folders/${folder.id}/download-list`);
            const dlToken = data.download_token || t;  // 30 天只读令牌，专用于下载清单
            const origin = window.location.origin;
            const lines = [
                `# TelePlay 下载清单 · ${data.folder.name}`,
                `# 文件数：${data.count}　总大小：${(data.total_bytes / 1073741824).toFixed(2)} GB`,
                '# 用法（aria2）：aria2c -i 本文件.txt --dir=D:\\目标目录 -x 8 -s 8 -j 1',
                '# 令牌有效期 30 天（只读，仅用于下载）',
                '',
            ];
            for (const e of data.entries) {
                lines.push(`${origin}/api/stream/${e.id}?token=${dlToken}`);
                lines.push(`  out=${e.path}`);
                lines.push('');
            }
            const blob = new Blob([lines.join('\n')], { type: 'text/plain;charset=utf-8' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `${folder.name}_下载清单.txt`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            setTimeout(() => URL.revokeObjectURL(url), 1000);
        } catch (e: any) {
            alert(e?.response?.data?.detail || '导出失败');
        }
    };

    const togglePrivate = async (folder: any) => {
        try {
            await api.patch(`/folders/${folder.id}`, { is_private: !folder.is_private });
            queryClient.invalidateQueries({ queryKey: ['folders'] });
            queryClient.invalidateQueries({ queryKey: ['folderTree'] });
            queryClient.invalidateQueries({ queryKey: ['files'] });
        } catch (e: any) {
            alert(e?.response?.data?.detail || '操作失败');
        }
    };

    // Close menu on escape
    useEffect(() => {
        const handleEscape = (e: KeyboardEvent) => {
            if (e.key === '退出') {
                e.stopPropagation();
                setActiveContextMenu(null);
            }
        };
        if (activeContextMenu) {
            document.addEventListener('keydown', handleEscape);
        }
        return () => document.removeEventListener('keydown', handleEscape);
    }, [activeContextMenu, setActiveContextMenu]);

    if (!activeContextMenu) return null;

    const { x, y } = activeContextMenu;
    const isMultiSelect = selectedFileIds.size > 1 && activeContextMenu.type === 'file' && selectedFileIds.has(activeContextMenu.item.id);

    // Adjust position to keep within viewport
    const getMenuPosition = () => {
        const menuWidth = 220;
        const menuHeight = 350; 
        const padding = 10;

        let posX = x;
        let posY = y;

        if (posX + menuWidth > window.innerWidth - padding) {
            posX = window.innerWidth - menuWidth - padding;
        }
        if (posY + menuHeight > window.innerHeight - padding) {
            posY = window.innerHeight - menuHeight - padding;
        }

        return { left: posX, top: posY };
    };

    const position = getMenuPosition();

    const handleAction = (action: () => void) => {
        action();
        setActiveContextMenu(null);
    };

    const handleCopy = async (text: string, id: string) => {
        const ok = await copyText(text);
        if (ok) {
            setCopiedId(id);
            setTimeout(() => setCopiedId(null), 2000);
        } else {
            addToast('复制失败：浏览器不允许访问剪贴板，请手动选中复制', 'error');
        }
    };

    // --- File Actions ---
    const handlePlay = (file: TelegramFile) => {
        setPreviewFile(file);
    };

    const handleShare = async (file: TelegramFile) => {
        try {
            const { data } = await api.post<TelegramFile>(`/files/${file.id}/share`);
            if (activeContextMenu && activeContextMenu.type === 'file') {
                setActiveContextMenu({ ...activeContextMenu, item: data });
            }
        } catch (error) {
            console.error('分享失败：', error);
        }
    };

    const handleRevokeShare = async (file: TelegramFile) => {
        try {
            const { data } = await api.delete<TelegramFile>(`/files/${file.id}/share`);
            if (activeContextMenu && activeContextMenu.type === 'file') {
                setActiveContextMenu({ ...activeContextMenu, item: data });
            }
        } catch (error) {
            console.error('撤销分享失败：', error);
        }
    };

    const ensurePublicLink = async (file: TelegramFile): Promise<string> => {
        if (file.public_stream_url) {
            return `${window.location.protocol}//${window.location.host}${file.public_stream_url}`;
        }
        try {
            const { data } = await api.post<TelegramFile>(`/files/${file.id}/share`);
            if (data.public_stream_url) {
                if (activeContextMenu && activeContextMenu.type === 'file') {
                    setActiveContextMenu({ ...activeContextMenu, item: data });
                }
                return `${window.location.protocol}//${window.location.host}${data.public_stream_url}`;
            }
        } catch (err) {
            console.error('创建公开链接失败：', err);
        }
        const token = authToken();
        const downloadUrl = `${api.defaults.baseURL}/stream/${file.id}?token=${token}`;
        return downloadUrl.startsWith('http')
            ? downloadUrl
            : `${window.location.protocol}//${window.location.host}${downloadUrl}`;
    };

    const handleDownload = async (file: TelegramFile) => {
        try {
            const url = await ensurePublicLink(file);
            const downloadUrl = url + (url.includes('?') ? '&' : '?') + 'download=1';
            const a = document.createElement('a');
            a.href = downloadUrl;
            a.download = file.file_name;
            a.target = '_blank';
            a.rel = 'noopener noreferrer';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
        } catch (err) {
            console.error('下载失败：', err);
        }
    };

    // --- Render ---

    return (
        <>
            {/* Overlay to catch clicks outside */}
            <div
                className="fixed inset-0 z-[99998]"
                onClick={(e) => {
                    e.stopPropagation();
                    setActiveContextMenu(null);
                }}
            />

            {/* Context Menu */}
            <div
                ref={menuRef}
                className="fixed bg-dark-800/95 backdrop-blur-xl border border-white/[0.08] rounded-xl shadow-2xl py-1.5 min-w-[220px] z-[99999] animate-scale-in"
                style={{
                    left: position.left,
                    top: position.top,
                    transformOrigin: 'top left'
                }}
                onClick={(e) => e.stopPropagation()}
                onContextMenu={(e) => e.preventDefault()}
            >
                {activeContextMenu.type === 'file' ? (
                    // File Context Menu
                    <>
                        {isMultiSelect ? (
                            <>
                                <div className="px-3 py-2 text-xs font-medium text-dark-400 uppercase tracking-wider">
                                    {selectedFileIds.size} Selected
                                </div>
                                <button className="context-menu-item w-full text-left" onClick={() => handleAction(() => setMoveFiles(selectedFiles))} data-admin-only="1">
                                    <FolderInput className="w-4 h-4" />
                                    Move ({selectedFileIds.size}) Items
                                </button>
                                <button className="context-menu-item w-full text-left text-red-400 hover:bg-red-500/10" onClick={() => handleAction(() => setDeleteConfirm({ type: 'file', items: Array.from(selectedFileIds).map(id => ({ id } as any)) }))} data-admin-only="1"> 
                                    <Trash2 className="w-4 h-4" />
                                    Delete ({selectedFileIds.size}) Items
                                </button>
                            </>
                        ) : (
                            <>
                                {(activeContextMenu.item.file_type === 'video' || activeContextMenu.item.file_type === 'audio') && (
                                    <button className="context-menu-item w-full text-left" onClick={() => handleAction(() => handlePlay(activeContextMenu.item as TelegramFile))}>
                                        <Play className="w-4 h-4" />
                                        播放
                                    </button>
                                )}
                                <button
                                    className="context-menu-item w-full text-left"
                                    onClick={() => { handleDownload(activeContextMenu.item as TelegramFile); setActiveContextMenu(null); }} data-admin-only="1">
                                    <Download className="w-4 h-4" />
                                    下载
                                </button>
                                
                                <hr className="border-white/[0.08] my-1" />

                                <button className="context-menu-item w-full text-left" onClick={async () => {
                                    const url = await ensurePublicLink(activeContextMenu.item as TelegramFile);
                                    handleCopy(url, 'stream');
                                }} data-admin-only="1">
                                    <Link className="w-4 h-4" />
                                    {copiedId === 'stream' ? '✓ Copied!' : '复制流地址'}
                                </button>

                                <button className="context-menu-item w-full text-left" onClick={async () => {
                                    const url = await ensurePublicLink(activeContextMenu.item as TelegramFile);
                                    const downloadUrl = url + (url.includes('?') ? '&' : '?') + 'download=1';
                                    handleCopy(downloadUrl, 'download');
                                }} data-admin-only="1">
                                    <HardDriveDownload className="w-4 h-4" />
                                    {copiedId === 'download' ? '✓ Copied!' : '复制下载地址'}
                                </button>

                                <hr className="border-white/[0.08] my-1" />

                                {(activeContextMenu.item as TelegramFile).public_stream_url ? (
                                    <>
                                        <button className="context-menu-item w-full text-left" onClick={() => handleCopy(`${window.location.protocol}//${window.location.host}${(activeContextMenu.item as TelegramFile).public_stream_url}`, 'public')}>
                                            <Globe className="w-4 h-4 text-emerald-400" />
                                            {copiedId === 'public' ? '✓ Copied!' : '复制公开链接'}
                                        </button>
                                        <button className="context-menu-item w-full text-left text-orange-400 hover:bg-orange-500/10" onClick={() => handleRevokeShare(activeContextMenu.item as TelegramFile)}>
                                            <ShieldOff className="w-4 h-4" />
                                            撤销公开链接
                                        </button>
                                    </>
                                ) : (
                                    <button className="context-menu-item w-full text-left" onClick={() => handleShare(activeContextMenu.item as TelegramFile)} data-admin-only="1">
                                        <Globe className="w-4 h-4" />
                                        创建公开链接
                                    </button>
                                )}

                                <hr className="border-white/[0.08] my-1" />
                                
                                <button className="context-menu-item w-full text-left" onClick={() => handleAction(() => setRenameFile(activeContextMenu.item as TelegramFile))} data-admin-only="1">
                                    <Edit className="w-4 h-4" />
                                    重命名
                                </button>
                                <button className="context-menu-item w-full text-left" onClick={() => handleAction(() => setMoveItems({ files: [activeContextMenu.item as TelegramFile], folders: [] }))} data-admin-only="1">
                                    <FolderInput className="w-4 h-4" />
                                    移动到…
                                </button>
                                
                                <hr className="border-white/[0.08] my-1" />
                                
                                <button className="context-menu-item w-full text-left text-red-400 hover:bg-red-500/10" onClick={() => handleAction(() => setDeleteConfirm({ type: 'file', items: [activeContextMenu.item] }))} data-admin-only="1">
                                    <Trash2 className="w-4 h-4" />
                                    删除
                                </button>
                            </>
                        )}
                    </>
                ) : (
                    // Folder Context Menu
                    <>
                        <button
                            className="context-menu-item w-full text-left"
                            onClick={() => handleAction(() => buildDownloadList(activeContextMenu.item))}
                        >
                            <Download className="w-4 h-4" />
                            下载清单（aria2 / IDM）
                        </button>
                        <button
                            className="context-menu-item w-full text-left"
                            data-admin-only="1"
                            onClick={() => handleAction(() => togglePrivate(activeContextMenu.item))}
                        >
                            <Lock className="w-4 h-4" />
                            {(activeContextMenu.item as any).is_private ? '取消私有' : '设为私有'}
                        </button>
                        <button
                            className="context-menu-item w-full text-left"
                            onClick={() => handleAction(() => setMoveItems({
                                files: [],
                                folders: [activeContextMenu.item as Folder],
                            }))} data-admin-only="1">
                            <FolderInput className="w-4 h-4" />
                            移动到…（含子目录与文件）
                        </button>
                        <hr className="border-white/[0.08] my-1" />
                        <button
                            className="context-menu-item w-full text-left"
                            onClick={() => handleAction(() => setRenameFolder(activeContextMenu.item as Folder))} data-admin-only="1">
                            <Edit className="w-4 h-4" />
                            重命名
                        </button>
                        <hr className="border-white/[0.08] my-1" />
                        <button
                            className="context-menu-item w-full text-left text-red-400 hover:bg-red-500/10"
                            onClick={() => handleAction(() => setDeleteConfirm({ type: 'folder', items: [activeContextMenu.item] }))} data-admin-only="1">
                            <Trash2 className="w-4 h-4" />
                            删除
                        </button>
                    </>
                )}
            </div>
        </>
    );
}