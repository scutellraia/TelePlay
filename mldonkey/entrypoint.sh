#!/bin/bash
# mldonkey 启动脚本：生成/修补配置 → 后台运行 mlnet（3.1.6 已移除 -daemon 参数）
set -e

HOME_DIR=/var/lib/mldonkey
INCOMING=/downloads/ed2k/incoming/files
TEMP_DIR=/downloads/ed2k/incoming/temp
DONE_DIR=/downloads/ed2k/done

mkdir -p "$HOME_DIR" "$INCOMING" "$TEMP_DIR" "$DONE_DIR"
chmod 777 "$DONE_DIR" 2>/dev/null || true
# ed2k 客户端端口固定 4662（mldonkey 默认 7633，会和端口映射/防火墙对不上）
if [ -f "$HOME_DIR/donkey.ini" ]; then
    sed -i -E 's|^[[:space:]]*port[[:space:]]*=.*|port = 4662|' "$HOME_DIR/donkey.ini"
fi

chown -R mldonkey:mldonkey "$HOME_DIR" /downloads/ed2k 2>/dev/null || true

start_core() {
    runuser -u mldonkey -- sh -c "cd $HOME_DIR && nohup /usr/bin/mlnet > $HOME_DIR/mlnet.log 2>&1 &"
}

if [ ! -f "$HOME_DIR/downloads.ini" ]; then
    echo "[entrypoint] generating default config..."
    start_core
    for i in $(seq 1 40); do
        [ -f "$HOME_DIR/downloads.ini" ] && break
        sleep 1
    done
    sleep 2
    pkill -u mldonkey mlnet 2>/dev/null || true
    sleep 1
fi

set_array_opt() {
    # 数组型选项（如 allowed_ips = [ "a"; "b";]）可能跨多行，必须整块替换
    key="$1"; value="$2"
    awk -v key="$key" -v val="$3" '
      index($0, key " =") == 1 { print val; skip=1; next }
      skip && /\]/ { skip=0; next }
      skip { next }
      { print }
    ' "$HOME_DIR/downloads.ini" > "$HOME_DIR/downloads.ini.new" \
      && mv "$HOME_DIR/downloads.ini.new" "$HOME_DIR/downloads.ini"
}

set_opt() {
    key="$1"; value="$2"
    if grep -qE "^[[:space:]]*${key}[[:space:]]*=" "$HOME_DIR/downloads.ini"; then
        sed -i -E "s|^[[:space:]]*${key}[[:space:]]*=.*|${key} = ${value}|" "$HOME_DIR/downloads.ini"
    else
        printf '\n%s = %s\n' "$key" "$value" >> "$HOME_DIR/downloads.ini"
    fi
}

set_opt http_port 4080
set_opt http_root_url '"/mldonkey/"'
set_opt telnet_port 4000
set_opt port 4662
set_opt incoming_directory "\"$INCOMING\""
set_opt temp_directory "\"$TEMP_DIR\""
set_opt enable_kademlia true
set_opt enable_servers true
set_array_opt allowed_ips '[ "0.0.0.0/0" ]'
set_opt max_hard_download_rate 0
set_opt max_hard_upload_rate 0
set_opt max_hard_upload_rate 10
set_opt max_hard_upload_rate_2 10
set_opt max_concurrent_downloads 5
set_opt auto_commit false
set_opt file_completed_cmd '"/usr/local/bin/on_complete.sh"'

chown -R mldonkey:mldonkey "$HOME_DIR" "$INCOMING" "$TEMP_DIR" 2>/dev/null || true

echo "[entrypoint] starting mlnet..."
start_core
sleep 5
if ! pgrep -u mldonkey mlnet >/dev/null 2>&1; then
    echo "[entrypoint] mlnet not running, log:"
    tail -30 "$HOME_DIR/mlnet.log" || true
    exit 1
fi
echo "[entrypoint] mlnet running"

# 载入额外的 ed2k 服务器列表（默认只配了一个，源太少）
for url in "http://upd.emule-security.org/server.met" "http://www.gruk.org/server.met.gz"; do
    curl -s -m 25 "http://127.0.0.1:4080/submit?q=$(printf 'servers %s' "$url" | sed 's/:/%3A/g; s|/|%2F|g; s/ /%20/g')" >/dev/null 2>&1 || true
    sleep 2
done
echo "[entrypoint] server lists requested"

tail -f /dev/null
