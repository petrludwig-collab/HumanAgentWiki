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

# done ----------------------------------------------------------------------
step "Done!"
cat <<EOF

  ${B}${G}HumanAgentWiki is installed${X}  ->  $DIR

  Next steps:
    cd $DIR
    ./haw index        # index your notes (put Markdown under ./notes)
    ./haw web          # web UI at http://127.0.0.1:8808
    ./haw serve        # MCP server for your AI agents

  Or try the bundled demo right now:
    NOTES_DIR=./sample_notes ./haw index && ./haw web

EOF
