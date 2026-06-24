#!/usr/bin/env bash
#
# HumanAgentWiki installer.
#
#   curl -fsSL https://raw.githubusercontent.com/petrludwig-collab/HumanAgentWiki/main/install.sh | bash
#
# Overridable via env: HAW_DIR (install path), HAW_REPO (git url), HAW_DB (db name).
set -euo pipefail

REPO="${HAW_REPO:-https://github.com/petrludwig-collab/HumanAgentWiki.git}"
DIR="${HAW_DIR:-$HOME/humanagentwiki}"
DB="${HAW_DB:-humanagentwiki}"

if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; C=$'\033[36m'; X=$'\033[0m'
else B=; G=; Y=; R=; C=; X=; fi
ok()   { printf "  ${G}OK${X} %s\n" "$*"; }
warn() { printf "  ${Y}!${X}  %s\n" "$*"; }
die()  { printf "  ${R}x  %s${X}\n" "$*" >&2; exit 1; }
step() { printf "\n${B}${C}==>${X} ${B}%s${X}\n" "$*"; }
# Prompt the user even under `curl | bash` (stdin is the pipe) by reading /dev/tty.
# No tty (CI / headless) -> return the default, so non-interactive installs still work.
ask() {  # $1=prompt  $2=default  -> echoes the answer
  local a=""
  if [ -e /dev/tty ]; then printf "%s" "$1" >/dev/tty; IFS= read -r a </dev/tty || a=""; fi
  printf "%s" "${a:-$2}"
}
lc() { printf "%s" "$1" | tr "[:upper:]" "[:lower:]"; }
# Read a secret from /dev/tty without echoing it; show one '*' per character (with backspace).
ask_secret() {  # $1=prompt  -> echoes the typed secret
  local prompt="$1" pw="" ch
  [ -e /dev/tty ] || { printf ""; return; }
  printf "%s" "$prompt" >/dev/tty
  while IFS= read -rsn1 ch </dev/tty; do
    [ -z "$ch" ] && break                                   # Enter -> done
    if [ "$ch" = $'\177' ] || [ "$ch" = $'\b' ]; then       # backspace
      [ -n "$pw" ] && { pw="${pw%?}"; printf '\b \b' >/dev/tty; }
    else
      pw="$pw$ch"; printf '*' >/dev/tty
    fi
  done
  printf '\n' >/dev/tty
  printf '%s' "$pw"
}

printf "${B}${C}"
cat <<'BANNER'
  ============================================================
   HumanAgentWiki
   Your second brain - local, private, agent-ready (MCP + RAG)
  ============================================================
BANNER
printf "${X}"

# 1) prerequisites ----------------------------------------------------------
step "Checking prerequisites"
command -v git >/dev/null || die "git is required"
ok "git"
PY=""
for c in python3.13 python3.12 python3.11 python3; do
  command -v "$c" >/dev/null || continue
  v=$("$c" -c 'import sys; print("%d %d" % sys.version_info[:2])' 2>/dev/null) || continue
  set -- $v
  if [ "$1" = "3" ] && [ "$2" -ge 10 ]; then PY="$c"; break; fi
done
[ -n "$PY" ] || die "Python 3.10+ is required"
ok "python ($("$PY" --version 2>&1))"

# 2) fetch the code ---------------------------------------------------------
step "Fetching HumanAgentWiki"
if [ -f "./cli.py" ] && [ -f "./requirements.txt" ]; then
  DIR="$(pwd)"; ok "using current checkout: $DIR"
elif [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only -q; ok "updated $DIR"
else
  git clone -q "$REPO" "$DIR"; ok "cloned into $DIR"
fi
cd "$DIR"

# 3) python environment -----------------------------------------------------
step "Installing Python dependencies (first run downloads PyTorch - be patient)"
# Debian/Ubuntu ship Python without venv support (no ensurepip). Detect and fix it.
if ! "$PY" -c "import ensurepip" >/dev/null 2>&1; then
  pyver="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  warn "Python venv support (ensurepip) is missing - installing python${pyver}-venv (needs sudo)"
  if command -v apt-get >/dev/null; then
    sudo apt-get update -qq >/dev/null 2>&1 || true
    sudo apt-get install -y "python${pyver}-venv" >/dev/null 2>&1 || sudo apt-get install -y python3-venv >/dev/null 2>&1 || true
  fi
  "$PY" -c "import ensurepip" >/dev/null 2>&1 \
    || die "Could not enable venv. Run:  sudo apt install python${pyver}-venv   then re-run this installer."
  ok "venv support installed"
fi
# (Re)create the venv if it's missing or incomplete. A first run that failed before
# ensurepip was available leaves a venv with python but NO pip — detect that by probing
# pip itself (not just the python binary) and rebuild.
if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
  rm -rf .venv; "$PY" -m venv .venv
fi
.venv/bin/python -m pip install -q --upgrade pip
# PyTorch + deps are a large download, and pip can sit SILENT for minutes while it resolves
# dependencies before any bytes move — that looks frozen. Run pip in the background and show a
# live heartbeat (elapsed time + .venv size + what pip is currently fetching) so the user always
# sees movement, even during the quiet resolver phase.
warn "downloading PyTorch + dependencies — the big step, several minutes (live progress below)"
PIPLOG="$(mktemp)"
# Disable errexit/pipefail for the heartbeat: the grep below finds nothing early on (empty log),
# which under `set -o pipefail` makes the whole pipeline non-zero and would otherwise abort the
# script SILENTLY mid-install. pip's real exit code is captured explicitly via `wait` instead.
# `</dev/null` also matters under `curl | bash`: a backgrounded job otherwise inherits the
# script-pipe as stdin and drains it, making bash hit EOF and stop.
set +e +o pipefail
.venv/bin/python -m pip install --progress-bar on -r requirements.txt >"$PIPLOG" 2>&1 </dev/null &
pip_pid=$!
secs=0
while kill -0 "$pip_pid" 2>/dev/null; do
  cur="$(grep -aoE '(Collecting|Downloading|Using cached|Building|Installing|Preparing|Successfully)[[:print:]]*' "$PIPLOG" 2>/dev/null | tail -1)"
  dl="$(du -sh .venv 2>/dev/null | cut -f1)"
  printf '\r   %s⏳%s %4ds  .venv:%-6s  %-46.46s' "$C" "$X" "$secs" "${dl:-?}" "${cur:-resolving dependencies…}"
  sleep 5; secs=$((secs + 5))
done
wait "$pip_pid"; piprc=$?
set -e -o pipefail
printf '\r%-90s\r' ' '
if [ "$piprc" -ne 0 ]; then printf '\n'; tail -25 "$PIPLOG"; rm -f "$PIPLOG"; die "pip install failed (log above)"; fi
rm -f "$PIPLOG"
ok "dependencies installed"

# 4) database (Postgres + pgvector) -----------------------------------------
step "Setting up PostgreSQL (pgvector)"
DB_URL="dbname=$DB"
pg_ready() { command -v pg_isready >/dev/null 2>&1 && pg_isready -q 2>/dev/null; }
if command -v docker >/dev/null && docker info >/dev/null 2>&1 && [ -f docker-compose.yml ]; then
  docker compose up -d >/dev/null 2>&1 || sudo docker compose up -d >/dev/null 2>&1 || true
  DB_URL="dbname=humanagentwiki user=humanagentwiki password=humanagentwiki host=localhost port=5432"
  for _ in $(seq 1 30); do
    .venv/bin/python -c "import psycopg; psycopg.connect('$DB_URL').close()" 2>/dev/null && break; sleep 1
  done
  ok "Postgres running via docker compose (pgvector image)"
elif pg_ready; then
  ok "using the PostgreSQL already running on this machine"
elif command -v apt-get >/dev/null; then
  warn "No PostgreSQL found - installing PostgreSQL + pgvector (needs sudo)"
  sudo apt-get update -qq >/dev/null 2>&1 || true
  sudo apt-get install -y postgresql postgresql-contrib >/dev/null 2>&1 || warn "postgres install hit an issue"
  PGMAJ="$(ls /usr/lib/postgresql/ 2>/dev/null | sort -n | tail -1)"
  sudo apt-get install -y "postgresql-${PGMAJ}-pgvector" >/dev/null 2>&1 \
    || sudo apt-get install -y postgresql-pgvector >/dev/null 2>&1 \
    || warn "pgvector apt package not found - schema may fail (alternative: install Docker and re-run)"
  sudo systemctl enable --now postgresql >/dev/null 2>&1 || true
  ok "PostgreSQL installed"
else
  warn "No PostgreSQL, Docker, or apt - install PostgreSQL+pgvector manually, then re-run."
fi
# For a local/native Postgres: give this OS user a role + the database (peer auth on the socket).
if pg_ready && [ "$DB_URL" = "dbname=$DB" ]; then
  sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$USER'" 2>/dev/null | grep -q 1 \
    || sudo -u postgres createuser -s "$USER" 2>/dev/null || true
  createdb "$DB" 2>/dev/null || true
  .venv/bin/python -c "import psycopg; psycopg.connect('dbname=$DB').close()" 2>/dev/null \
    && ok "database '$DB' ready" || warn "could not connect to '$DB' yet"
fi

# 5) configuration: DB url + RAM-adaptive embedding model -------------------
step "Writing configuration"
[ -f .env ] || cp .env.example .env
tmp="$(mktemp)"; sed "s#^DATABASE_URL=.*#DATABASE_URL=$DB_URL#" .env > "$tmp" && mv "$tmp" .env
# Pick the embedding model by available RAM. BGE-M3 is excellent for Czech but needs ~2.3 GB to
# load (and serve + web each load a copy). On small servers fall back to a light multilingual
# model (still handles Czech + 100 languages) that loads in a fraction of the RAM.
RAM_MB="$(free -m 2>/dev/null | awk '/^Mem:/{print $2}')"; RAM_MB="${RAM_MB:-0}"
if [ "$RAM_MB" -lt 6000 ]; then
  EMB_MODEL="intfloat/multilingual-e5-small"; EMB_DIM="384"
  warn "small server (${RAM_MB} MB RAM) - using a lighter multilingual model (e5-small, 384d)"
else
  EMB_MODEL="BAAI/bge-m3"; EMB_DIM="1024"
  ok "using BGE-M3 (server has ${RAM_MB} MB RAM)"
fi
for kv in "EMBED_MODEL=$EMB_MODEL" "EMBED_DIM=$EMB_DIM"; do
  k="${kv%%=*}"
  if grep -q "^${k}=" .env 2>/dev/null; then
    tmp="$(mktemp)"; sed "s#^${k}=.*#${kv}#" .env > "$tmp" && mv "$tmp" .env
  else echo "$kv" >> .env; fi
done
ok "wrote .env"
chmod +x haw 2>/dev/null || true

# 5b) swap — safety net so the embedding model loads when free RAM is tight --
SWAP_KB="$(free -k 2>/dev/null | awk '/^Swap:/{print $2}')"; SWAP_KB="${SWAP_KB:-0}"
if [ "$SWAP_KB" -eq 0 ] && [ "$RAM_MB" -lt 8000 ] && [ ! -e /swapfile ] && command -v sudo >/dev/null; then
  step "Adding 4 GB swap (safety for model loading on a small server)"
  if { sudo fallocate -l 4G /swapfile 2>/dev/null || sudo dd if=/dev/zero of=/swapfile bs=1M count=4096 status=none 2>/dev/null; } \
     && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null 2>&1 && sudo swapon /swapfile 2>/dev/null; then
    grep -q '/swapfile' /etc/fstab 2>/dev/null || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
    ok "4 GB swap active (survives reboot)"
  else warn "could not add swap (continuing anyway)"; fi
fi

# 6) schema -----------------------------------------------------------------
if command -v pg_isready >/dev/null && pg_isready -q 2>/dev/null \
   || { command -v docker >/dev/null && docker info >/dev/null 2>&1; }; then
  step "Creating database schema"
  set -a; . ./.env; set +a
  # If a chunks table already exists with a different vector dimension (model changed), drop it
  # so the schema is recreated for the new dim. The index step repopulates from your notes.
  CUR_DIM="$(.venv/bin/python - <<'PY' 2>/dev/null
import os, psycopg
try:
    c = psycopg.connect(os.environ["DATABASE_URL"]); cur = c.cursor()
    cur.execute("select a.atttypmod from pg_attribute a join pg_class c on a.attrelid=c.oid "
                "where c.relname='chunks' and a.attname='embedding'")
    r = cur.fetchone(); print(r[0] if r and r[0] > 0 else "")
except Exception:
    print("")
PY
)"
  if [ -n "$CUR_DIM" ] && [ "$CUR_DIM" != "${EMBED_DIM:-1024}" ]; then
    warn "embedding dimension changed ($CUR_DIM -> ${EMBED_DIM}) - recreating schema"
    .venv/bin/python -c "import os,psycopg;c=psycopg.connect(os.environ['DATABASE_URL']);cur=c.cursor();cur.execute('DROP TABLE IF EXISTS chunks CASCADE');cur.execute('DROP TABLE IF EXISTS files CASCADE');c.commit()" 2>/dev/null || true
  fi
  .venv/bin/python cli.py init-db && ok "schema ready"
fi

# 7) notes folder (empty, or seeded with bundled examples) ------------------
step "Notes folder"
NOTES_VAL="$DIR/notes"
mkdir -p "$NOTES_VAL"
seed="$(ask "  Seed with the bundled example notes so the graph isn't empty? [Y/n]: " "Y")"
if [ "$(lc "$seed")" != "n" ] && [ -d "$DIR/sample_notes" ]; then
  cp -R "$DIR/sample_notes/." "$NOTES_VAL/" 2>/dev/null || true
  ok "seeded example notes -> notes/"
else
  ok "empty notes/ folder (drop your Markdown here)"
fi
[ -d "$NOTES_VAL/.git" ] || git -C "$NOTES_VAL" init -q 2>/dev/null || true
if grep -q '^NOTES_DIR=' .env 2>/dev/null; then
  tmp="$(mktemp)"; sed "s#^NOTES_DIR=.*#NOTES_DIR=$NOTES_VAL#" .env > "$tmp" && mv "$tmp" .env
else echo "NOTES_DIR=$NOTES_VAL" >> .env; fi

# 8) index the notes --------------------------------------------------------
if command -v pg_isready >/dev/null && pg_isready -q 2>/dev/null \
   || { command -v docker >/dev/null && docker info >/dev/null 2>&1; }; then
  step "Indexing notes (first run loads the embedding model - be patient)"
  set -a; . ./.env; set +a
  .venv/bin/python cli.py index && ok "notes indexed"
fi

# 9) connect ALL this server's AI agents via MCP ----------------------------
step "Connect AI agents (MCP)"
MCP_PORT_VAL="$( ( set -a; . ./.env 2>/dev/null; set +a; echo "${MCP_PORT:-8802}" ) )"
MCP_URL="http://127.0.0.1:${MCP_PORT_VAL}/mcp"
NAME="${HAW_MCP_NAME:-humanagentwiki}"
wire="$(ask "  Point ALL AI agents on this server (Claude / Codex / Hermes / OpenClaw) at the wiki? [Y/n]: " "Y")"
if [ "$(lc "$wire")" != "n" ]; then
  any=0
  if command -v claude >/dev/null; then
    claude mcp add -s user --transport http "$NAME" "$MCP_URL" >/dev/null 2>&1 \
      && { ok "Claude Code -> $NAME"; any=1; } || warn "Claude Code: skipped (maybe already set)"
  fi
  if command -v codex >/dev/null; then
    codex mcp add "$NAME" --url "$MCP_URL" >/dev/null 2>&1 || true   # idempotent; verify below
    if codex mcp list 2>/dev/null | grep -q "^$NAME\b\|[[:space:]]$NAME[[:space:]]"; then
      ok "Codex -> $NAME"; any=1
    else warn "Codex: couldn't add (try: codex mcp add $NAME --url $MCP_URL)"; fi
  fi
  # Hermes: its 'mcp add' prompts on the tty for an optional token (hangs / aborts when scripted).
  # Write the server straight into its config instead — non-interactive and persistent.
  if command -v hermes >/dev/null; then
    if hermes config set "mcp_servers.$NAME.url" "$MCP_URL" >/dev/null 2>&1 \
       && hermes config set "mcp_servers.$NAME.enabled" true >/dev/null 2>&1; then
      ok "Hermes -> $NAME"; any=1
    else warn "Hermes: couldn't set MCP (add manually: hermes mcp add $NAME --url $MCP_URL)"; fi
  fi
  # OpenClaw probes the URL before saving; the wiki MCP is FastMCP streamable-HTTP at /mcp,
  # so we MUST pass --transport streamable-http or the probe fails and nothing is saved.
  if command -v openclaw >/dev/null; then
    if openclaw mcp add "$NAME" --url "$MCP_URL" --transport streamable-http </dev/null >/dev/null 2>&1; then
      ok "OpenClaw -> $NAME"; any=1
    elif openclaw mcp list 2>/dev/null | grep -q "[[:space:]]$NAME$\|^- $NAME$"; then
      ok "OpenClaw -> $NAME (already set)"; any=1
    else
      warn "OpenClaw: couldn't add (try: openclaw mcp add $NAME --url $MCP_URL --transport streamable-http)"
    fi
  fi
  if [ "$any" = 1 ]; then
    ok "agents can use: brain_search / brain_get / brain_neighbors"
    # Gateway-type agents (Hermes/OpenClaw) are reloaded in step 12. Codex/Claude are interactive
    # REPL sessions we can't restart for you.
    command -v codex  >/dev/null && warn "Codex: restart a running session to load the wiki (new sessions get it automatically)"
    command -v claude >/dev/null && warn "Claude: restart a running session to load the wiki (new sessions get it automatically)"
  else warn "no agent CLI found on PATH - register $MCP_URL manually in each agent"; fi
else
  warn "agents not wired - the MCP endpoint is $MCP_URL"
fi

# 10) network reach: localhost only, or visible from outside ----------------
step "Web UI access"
WEB_PORT_VAL="$( ( set -a; . ./.env 2>/dev/null; set +a; echo "${WEB_PORT:-8808}" ) )"
echo "  The web UI is always reachable on this machine (http://127.0.0.1:$WEB_PORT_VAL)."
vis="$(ask "  Also make it visible from outside - the server's IP / the internet? [y/N]: " "N")"
if [ "$(lc "$vis")" = "y" ]; then WEB_HOST_VAL="0.0.0.0"; else WEB_HOST_VAL="127.0.0.1"; fi
if grep -q '^WEB_HOST=' .env 2>/dev/null; then
  tmp="$(mktemp)"; sed "s#^WEB_HOST=.*#WEB_HOST=$WEB_HOST_VAL#" .env > "$tmp" && mv "$tmp" .env
else echo "WEB_HOST=$WEB_HOST_VAL" >> .env; fi
if [ "$WEB_HOST_VAL" = "0.0.0.0" ]; then warn "visible from outside - set a password below (strongly recommended)"
else ok "kept private (127.0.0.1 only)"; fi

# 11) HTTP auth (defaults to YES when the UI is visible from outside) --------
step "Web UI password (HTTP auth)"
warn "The web UI shows ALL your notes."
if [ "$WEB_HOST_VAL" = "0.0.0.0" ]; then want="$(ask "  Protect it with a username:password? [Y/n]: " "Y")"
else want="$(ask "  Protect it with a username:password? [y/N]: " "N")"; fi
if [ "$(lc "$want")" = "y" ]; then
  u="$(ask "    username: " "")"
  p="$(ask_secret "    password: ")"
  if [ -n "$u" ] && [ -n "$p" ]; then
    cred="$u:$p"
    if grep -q '^WEB_AUTH=' .env 2>/dev/null; then
      tmp="$(mktemp)"; sed "s#^WEB_AUTH=.*#WEB_AUTH=$cred#" .env > "$tmp" && mv "$tmp" .env
    else echo "WEB_AUTH=$cred" >> .env; fi
    ok "web UI protected (login: $u / password hidden)"
  else warn "username or password empty - left open"; fi
else
  # "no password" must really mean open: strip any WEB_AUTH left by a previous run.
  if grep -q '^WEB_AUTH=' .env 2>/dev/null; then
    tmp="$(mktemp)"; grep -v '^WEB_AUTH=' .env > "$tmp" && mv "$tmp" .env
  fi
  warn "web UI left open (no password)"
fi
chmod 600 .env 2>/dev/null || true

# 12) (re)start it so the CURRENT config (model, host, auth) actually applies -
step "Starting HumanAgentWiki"
# Always restart: a running server still holds the OLD env (e.g. a previous password), so a
# re-run must kill + relaunch, not skip, or config changes silently don't take effect.
pkill -f "cli.py serve" 2>/dev/null || true
pkill -f "cli.py web"   2>/dev/null || true
# Wait until the old processes are really gone so their ports are free — a too-short sleep let the
# new web hit "address already in use" and exit immediately.
for _ in $(seq 1 15); do
  if pgrep -f "cli.py serve" >/dev/null 2>&1 || pgrep -f "cli.py web" >/dev/null 2>&1; then sleep 1; else break; fi
done
# Re-load .env so the FINAL config (WEB_HOST / WEB_AUTH chosen by the prompts above) is in the
# environment the servers inherit. An earlier `set -a; . ./.env` baked stale defaults (notably
# WEB_HOST=127.0.0.1) into this shell; since ./haw only sets vars NOT already in the environment,
# that stale value would otherwise shadow the updated file and the web would bind to localhost
# even when the user chose "visible from outside".
set -a; . ./.env; set +a
( nohup ./haw serve >/tmp/haw-serve.log 2>&1 </dev/null & disown ) 2>/dev/null || true
( nohup ./haw web   >/tmp/haw-web.log   2>&1 </dev/null & disown ) 2>/dev/null || true
sleep 3
if pgrep -f "cli.py web" >/dev/null 2>&1; then
  ok "MCP server + web UI (re)started (logs: /tmp/haw-serve.log, /tmp/haw-web.log)"
else
  warn "web didn't stay up - check /tmp/haw-web.log, then: cd $DIR && nohup ./haw web >/tmp/haw-web.log 2>&1 &"
fi
# Reload running agents so they pick up the wiki MCP now (they only read MCP at startup).
if [ "${any:-0}" = 1 ]; then
  step "Reloading running agents so they see the wiki"
  # Gateways: clean restart. setsid detaches from this terminal (no foreground hijack) and from
  # this script's process group (survives our exit); a supervisor reconciles to one instance.
  if command -v setsid >/dev/null 2>&1; then
    command -v hermes   >/dev/null 2>&1 && { setsid hermes gateway restart   </dev/null >/dev/null 2>&1 & ok "Hermes gateway reloaded"; }
    command -v openclaw >/dev/null 2>&1 && { setsid openclaw gateway restart </dev/null >/dev/null 2>&1 & ok "OpenClaw gateway reloaded"; }
  fi
  # Codex/Claude have no hot-reload and no gateway. We deliberately DON'T kill them: a blind kill
  # took an agent down with no respawn (supervisors vary / may not own it). They load the wiki on
  # their next restart — which the user's supervisor or the user does.
  command -v codex  >/dev/null 2>&1 && warn "Codex: restart its session to load the wiki (new sessions already have it)"
  command -v claude >/dev/null 2>&1 && warn "Claude: restart its session to load the wiki (new sessions already have it)"
fi
warn "for persistence after reboot, add './haw serve' and './haw web' to your boot/supervisor"

# done + link ---------------------------------------------------------------
PRIMARY_IP="$(.venv/bin/python - <<'PYIP'
import socket
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("1.1.1.1", 80))
    print(s.getsockname()[0]); s.close()
except Exception:
    print("127.0.0.1")
PYIP
)"
step "Done!"
printf "\n  ${B}${G}HumanAgentWiki is installed${X}  ->  %s\n" "$DIR"
printf "\n  ${B}Open the wiki:${X}\n"
if [ "$WEB_HOST_VAL" = "0.0.0.0" ]; then
  printf "    ${C}http://%s:%s${X}   (local: http://127.0.0.1:%s)\n" "$PRIMARY_IP" "$WEB_PORT_VAL" "$WEB_PORT_VAL"
else
  printf "    ${C}http://127.0.0.1:%s${X}\n" "$WEB_PORT_VAL"
fi
printf "\n  Agents query it via MCP at %s\n" "$MCP_URL"
printf "  Add your own notes as Markdown under %s   then  ./haw index\n\n" "$NOTES_VAL"
