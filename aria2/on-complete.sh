#!/bin/sh
# aria2 download-complete / download-error hook.
# aria2 passes: $1 = gid, $2 = number of files, $3 = path of the first file.
#
# We do not upload from here; we just tell the backend that this gid finished so
# it can push the file to Telegram and then delete it. The request is fire and
# forget: a failure here is recovered by the backend reconcile sweep.

GID="$1"
BACKEND="${UP_BACKEND_URL:-http://backend:8000}"
SECRET="${UP_HOOK_SECRET:-}"

if [ -z "$GID" ]; then
    exit 0
fi

curl -s -m 15 -X POST "${BACKEND}/up/api/hooks/complete" \
    -H "Content-Type: application/json" \
    -H "X-Hook-Secret: ${SECRET}" \
    -d "{\"gid\":\"${GID}\"}" >/dev/null 2>&1 || true

exit 0