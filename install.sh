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
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
ok "dependencies installed"

# 4) database (Postgres + pgvector) -----------------------------------------
step "Setting up PostgreSQL (pgvector)"
DB_URL="dbname=$DB"
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  docker compose up -d >/dev/null
  DB_URL="dbname=humanagentwiki user=humanagentwiki password=humanagentwiki host=localhost port=5432"
  for _ in $(seq 1 30); do
    if .venv/bin/python -c "import psycopg; psycopg.connect('$DB_URL').close()" 2>/dev/null; then break; fi
    sleep 1
  done
  ok "Postgres running via docker compose"
elif command -v createdb >/dev/null && command -v pg_isready >/dev/null && pg_isready -q 2>/dev/null; then
  createdb "$DB" 2>/dev/null || true
  ok "using local PostgreSQL (database '$DB')"
else
  warn "No running PostgreSQL found - schema step will be skipped."
  warn "Start one with:  docker compose up -d   (needs Docker), then re-run this script."
fi

# 5) configuration ----------------------------------------------------------
step "Writing configuration"
if [ ! -f .env ]; then
  cp .env.example .env
  tmp="$(mktemp)"; sed "s#^DATABASE_URL=.*#DATABASE_URL=$DB_URL#" .env > "$tmp" && mv "$tmp" .env
  ok "wrote .env"
else
  ok ".env already present (kept as-is)"
fi
chmod +x haw 2>/dev/null || true

# 6) schema -----------------------------------------------------------------
if command -v pg_isready >/dev/null && pg_isready -q 2>/dev/null \
   || { command -v docker >/dev/null && docker info >/dev/null 2>&1; }; then
  step "Creating database schema"
  set -a; . ./.env; set +a
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

# 8) HTTP auth for the web UI (recommended if reachable over LAN/VPN) --------
step "Web UI password (HTTP auth)"
warn "The web UI shows ALL notes. Protect it if it's reachable over LAN/VPN."
cred="$(ask "  Set username:password for the web UI? (blank = no password): " "")"
if [ -n "$cred" ]; then
  if grep -q '^WEB_AUTH=' .env 2>/dev/null; then
    tmp="$(mktemp)"; sed "s#^WEB_AUTH=.*#WEB_AUTH=$cred#" .env > "$tmp" && mv "$tmp" .env
  else echo "WEB_AUTH=$cred" >> .env; fi
  ok "web UI protected (WEB_AUTH set in .env)"
else
  warn "web UI left open (no password) - fine for 127.0.0.1 only"
fi
chmod 600 .env 2>/dev/null || true

# 9) index the notes --------------------------------------------------------
if command -v pg_isready >/dev/null && pg_isready -q 2>/dev/null \
   || { command -v docker >/dev/null && docker info >/dev/null 2>&1; }; then
  step "Indexing notes (first run loads the embedding model - be patient)"
  set -a; . ./.env; set +a
  .venv/bin/python cli.py index && ok "notes indexed"
fi

# 10) connect this server's AI agents via MCP -------------------------------
step "Connecting AI agents (MCP)"
MCP_PORT_VAL="$( ( set -a; . ./.env 2>/dev/null; set +a; echo "${MCP_PORT:-8802}" ) )"
MCP_URL="http://127.0.0.1:${MCP_PORT_VAL}/mcp"
NAME="${HAW_MCP_NAME:-brain}"
wire="$(ask "  Point the AI agents on this server (Claude/Codex/Hermes/OpenClaw) at the wiki? [Y/n]: " "Y")"
if [ "$(lc "$wire")" != "n" ]; then
  any=0
  if command -v claude >/dev/null; then
    claude mcp add -s user --transport http "$NAME" "$MCP_URL" >/dev/null 2>&1 \
      && { ok "Claude Code -> $NAME"; any=1; } || warn "Claude Code: skipped (maybe already set)"
  fi
  if command -v codex >/dev/null; then
    cfg="$HOME/.codex/config.toml"; mkdir -p "$HOME/.codex"; touch "$cfg"
    grep -q "\[mcp_servers.$NAME\]" "$cfg" 2>/dev/null \
      || printf '\n[mcp_servers.%s]\nurl = "%s"\n' "$NAME" "$MCP_URL" >> "$cfg"
    ok "Codex -> $NAME"; any=1
  fi
  if command -v hermes >/dev/null; then
    hermes mcp add "$NAME" --url "$MCP_URL" >/dev/null 2>&1 \
      && { ok "Hermes -> $NAME"; any=1; } || warn "Hermes: skipped (maybe already set)"
  fi
  if command -v openclaw >/dev/null; then
    openclaw mcp add "$NAME" --url "$MCP_URL" >/dev/null 2>&1 \
      && { ok "OpenClaw -> $NAME"; any=1; } || warn "OpenClaw: skipped (maybe already set)"
  fi
  if [ "$any" = 1 ]; then ok "agents can now use: brain_search / brain_get / brain_neighbors"
  else warn "no agent CLI found on PATH - register $MCP_URL manually in each agent"; fi
else
  warn "agents not wired - the MCP endpoint will be $MCP_URL"
fi

# done ----------------------------------------------------------------------
step "Done!"
cat <<EOF

  ${B}${G}HumanAgentWiki is installed${X}  ->  $DIR

  Start it (keep both running; add to your boot/supervisor for persistence):
    cd $DIR
    ./haw serve     # MCP server on $MCP_URL  (the agents talk to this)
    ./haw web       # web UI (uses WEB_AUTH from .env if you set a password)

  To reach the web UI over LAN/VPN, set WEB_HOST in .env (e.g. 0.0.0.0) - and
  keep WEB_AUTH set. Add notes as Markdown under $NOTES_VAL then  ./haw index

EOF
