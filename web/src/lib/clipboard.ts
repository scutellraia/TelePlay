/**
 * 复制文本到剪贴板。
 *
 * Clipboard API 只在安全上下文（https / localhost）存在；本项目常用 http 直连 IP 访问，
 * 那时 navigator.clipboard 是 undefined，会导致“点了复制却什么都没复制”。
 * 这里先试 Clipboard API，失败再回退到 textarea + execCommand('copy')。
 */
export async function copyText(text: string): Promise<boolean> {
    if (!text) return false;
    try {
        if (typeof navigator !== 'undefined' && navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(text);
            return true;
        }
    } catch {
        // 继续尝试回退方案
    }
    try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.top = '-1000px';
        ta.style.left = '-1000px';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        const prevFocus = document.activeElement as HTMLElement | null;
        ta.focus();
        ta.select();
        ta.setSelectionRange(0, text.length);
        const ok = document.execCommand('copy');
        document.body.removeChild(ta);
        if (prevFocus && typeof prevFocus.focus === 'function') prevFocus.focus();
        return ok;
    } catch {
        return false;
    }
}