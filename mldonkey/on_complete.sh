#!/bin/bash
# mldonkey 下载完成回调：把完成的临时文件搬到共享的 done 目录，交给后端自动上传
DONE=/downloads/ed2k/done
SRC="${TEMPNAME:-$1}"
NAME="${FILENAME:-$(basename "$SRC")}"
mkdir -p "$DONE" 2>/dev/null || true
if [ -n "$SRC" ] && [ -f "$SRC" ]; then
    mv -f "$SRC" "$DONE/$NAME" 2>/dev/null || cp -f "$SRC" "$DONE/$NAME" 2>/dev/null || true
fi
exit 0