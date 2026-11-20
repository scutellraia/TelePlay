/**
 * In-browser viewer for images and PDFs.
 *
 * Video/audio keep using MediaPlayer; this only handles documents the browser
 * can render itself (image/*, application/pdf) plus an "open in new tab" path
 * for anything else.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useAppStore } from '../lib/store';
import { authToken, useFiles } from '../lib/api';
import { ChevronLeft, ChevronRight } from 'lucide-react';

export default function DocViewer() {
    const { previewDoc, setPreviewDoc, currentFolderId } = useAppStore();
    const file = previewDoc;
    // 只有真正打开预览时才拉列表：全局挂载时若未登录触发 401 会被拦截器跳转，形成刷新循环
    const { data } = useFiles(previewDoc ? currentFolderId : undefined, undefined, undefined, 1, !!previewDoc);

    // 当前文件夹里的图片列表（用于左右切换）
    const images = useMemo(
        () => (data?.files || []).filter(
            (f) => f.file_type === 'image' || (f.mime_type || '').toLowerCase().startsWith('image/'),
        ),
        [data],
    );
    const idx = useMemo(
        () => (file ? images.findIndex((f) => f.id === file.id) : -1),
        [images, file],
    );
    const prev = idx > 0 ? images[idx - 1] : null;
    const next = idx >= 0 && idx < images.length - 1 ? images[idx + 1] : null;

    // 点击后立即给反馈：按钮变白，等新图加载完再恢复
    const [navBusy, setNavBusy] = useState<null | 'prev' | 'next'>(null);
    const clearBusy = useCallback(() => setNavBusy(null), []);

    useEffect(() => {
        if (!navBusy) return;
        const t = setTimeout(() => setNavBusy(null), 15000);
        return () => clearTimeout(t);
    }, [navBusy]);

    const go = useCallback(
        (target: any, dir: 'prev' | 'next') => {
            if (!target) return;
            setNavBusy(dir);
            setPreviewDoc(target);
        },
        [setPreviewDoc],
    );

    useEffect(() => {
        if (!file) return;
        const onKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') setPreviewDoc(null);
            else if (e.key === 'ArrowLeft' && prev) { setNavBusy('prev'); setPreviewDoc(prev); }
            else if (e.key === 'ArrowRight' && next) { setNavBusy('next'); setPreviewDoc(next); }
        };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [file, prev, next, setPreviewDoc]);

    if (!file) return null;

    const token = authToken();
    const url = `${file.stream_url}?token=${token}`;
    const mime = (file.mime_type || '').toLowerCase();
    const isImage = file.file_type === 'image' || mime.startsWith('image/');
    const isPdf = mime === 'application/pdf' || /\.pdf$/i.test(file.file_name || '');

    return (
        <div
            className="fixed inset-0 z-[110] bg-black/90 flex flex-col"
            onClick={() => setPreviewDoc(null)}
        >
            <div
                className="flex items-center justify-between gap-3 px-4 py-3 bg-black/60 border-b border-white/10"
                onClick={(e) => e.stopPropagation()}
            >
                <div className="text-sm text-gray-200 truncate">
                    {file.file_name}
                    {idx >= 0 && images.length > 1 && (
                        <span className="text-gray-400 ml-2">({idx + 1}/{images.length})</span>
                    )}
                </div>
                <div className="flex items-center gap-2 shrink-0">
                    <a href={url} target="_blank" rel="noreferrer"
                       className="px-3 py-1.5 rounded-lg bg-white/10 hover:bg-white/20 text-sm text-white">
                        新窗口打开
                    </a>
                    <a href={url} download={file.file_name}
                       className="px-3 py-1.5 rounded-lg bg-white/10 hover:bg-white/20 text-sm text-white">
                        下载
                    </a>
                    <button
                        className="px-3 py-1.5 rounded-lg bg-primary-600 hover:bg-primary-500 text-sm text-white"
                        onClick={() => setPreviewDoc(null)}
                    >
                        关闭
                    </button>
                </div>
            </div>
            <div className="flex-1 min-h-0 p-2 relative" onClick={(e) => e.stopPropagation()}>
                {isImage ? (
                    <img src={url} alt={file.file_name} className="w-full h-full object-contain" onLoad={clearBusy} onError={clearBusy} />
                ) : isPdf ? (
                    <iframe src={url} title={file.file_name} className="w-full h-full bg-white rounded-lg" />
                ) : (
                    <div className="h-full flex flex-col items-center justify-center gap-3 text-gray-300 text-sm">
                        <div>该文件类型不支持内嵌预览</div>
                        <a href={url} target="_blank" rel="noreferrer"
                           className="px-4 py-2 rounded-lg bg-primary-600 text-white">
                            在新窗口打开
                        </a>
                    </div>
                )}

                {/* 上一张 / 下一张 */}
                {isImage && (
                    <>
                        <button
                            onClick={(e) => { e.stopPropagation(); go(prev, 'prev'); }}
                            disabled={!prev}
                            title="上一张（←）"
                            className={`absolute left-4 top-1/2 -translate-y-1/2 w-12 h-12 rounded-full flex items-center justify-center transition-colors border disabled:opacity-25 disabled:cursor-default ${navBusy === 'prev' ? 'bg-white text-black border-white shadow-lg shadow-white/30' : 'bg-black/55 hover:bg-black/80 text-white border-white/20'}`}
                        >
                            <ChevronLeft className="w-7 h-7" />
                        </button>
                        <button
                            onClick={(e) => { e.stopPropagation(); go(next, 'next'); }}
                            disabled={!next}
                            title="下一张（→）"
                            className={`absolute right-4 top-1/2 -translate-y-1/2 w-12 h-12 rounded-full flex items-center justify-center transition-colors border disabled:opacity-25 disabled:cursor-default ${navBusy === 'next' ? 'bg-white text-black border-white shadow-lg shadow-white/30' : 'bg-black/55 hover:bg-black/80 text-white border-white/20'}`}
                        >
                            <ChevronRight className="w-7 h-7" />
                        </button>
                    </>
                )}
            </div>
        </div>
    );
}