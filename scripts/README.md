# Database Migration Scripts

This directory contains automation scripts for migrating PostgreSQL databases between environments.

## Scripts Overview

### 🎯 import_to_docker.sh
**Purpose:** Import an existing database dump into the local Docker PostgreSQL container

**Use when:**
- You already have a dump file from PgAdmin or pg_dump
- You want to restore a backup to your local environment
- You're setting up a new development environment

**Usage:**
```bash
bash scripts/import_to_docker.sh
```

**What it does:**
1. Checks Docker and PostgreSQL container are running
2. Detects dump file format (Plain SQL or Custom binary)
3. Stops FastAPI application (if running)
4. Terminates active database connections
5. Drops and recreates the database
6. Runs Alembic migrations (for data-only dumps)
7. Imports the dump file
8. Verifies the import with table counts
9. Shows next steps

**Requirements:**
- Dump file at: `backend/data/hostlib_backup` (or similar name)
- Docker Desktop running
- postgres_db container running

---

### 🌐 dump_from_server.sh
**Purpose:** Create a database dump from a remote PostgreSQL server

**Use when:**
- You need to export data from a remote server
- You don't have access to PgAdmin GUI
- You want to automate dump creation

**Usage:**
```bash
bash scripts/dump_from_server.sh
```

**What it does:**
1. Prompts for server connection details (host, port, database, user, password)
2. Tests the connection
3. Asks for dump format (Plain SQL, Custom, or Data-only)
4. Creates the dump using pg_dump
5. Verifies the dump file
6. Saves it to `backend/data/hostlib_backup`

**Requirements:**
- Network access to the remote server
- Valid database credentials
- pg_dump installed (or Docker container running)

---

### 🧙 migrate_database.sh
**Purpose:** Complete migration wizard that guides you through the entire process

**Use when:**
- You're new to the project and need guidance
- You want a streamlined, interactive experience
- You're not sure which script to use

**Usage:**
```bash
bash scripts/migrate_database.sh
```

**What it does:**
1. Asks how you want to proceed:
   - Option 1: Use existing dump file
   - Option 2: Create new dump from server
2. Runs the appropriate script(s)
3. Shows a comprehensive summary
4. Provides next steps

**Requirements:**
- Same as import_to_docker.sh or dump_from_server.sh (depending on choice)

---

## Quick Reference

### I have a dump file already
```bash
# Put your file here:
# backend/data/hostlib_backup

# Then run:
bash scripts/import_to_docker.sh
```

### I need to create a dump from the server
```bash
# Option 1: Interactive dump creation
bash scripts/dump_from_server.sh

# Option 2: Manual pg_dump
PGPASSWORD='password' pg_dump -h server_host -U username -d database \
  --clean --inserts --no-owner --no-privileges \
  -f backend/data/hostlib_backup
```

### I want the full guided experience
```bash
bash scripts/migrate_database.sh
```

---

## File Locations

The scripts expect dump files in these locations:
- `backend/data/hostlib_backup` (recommended)
- `backend/data/server_dump.sql`
- `backend/data/server_dump.dump`

Output logs and backups will be saved to:
- `backend/data/local_backup_YYYYMMDD_HHMMSS.sql` (if backup created)

---

## Environment

All scripts are designed to work with:
- **Platform:** Git Bash on Windows (MinGW64)
- **Docker:** Docker Desktop with postgres:15 container
- **Database:** PostgreSQL 15
- **Local DB:** hostlib_db (user: diakonx)

---

## Common Tasks

### Check if database is running
```bash
docker ps | grep postgres_db
```

### Start database
```bash
docker compose up -d db_v2
```

### Stop application before import
```bash
docker-compose stop fastapi
```

### Connect to database manually
```bash
docker exec -it postgres_db psql -U diakonx -d hostlib_db
```

### View import logs
The scripts output detailed logs to the console. For troubleshooting, you can:
```bash
# Run with debug output
bash -x scripts/import_to_docker.sh 2>&1 | tee import.log
```

### Test a small import first
If your dump is very large, you can test with a smaller subset:
```bash
# Extract first 1000 lines
head -n 1000 backend/data/hostlib_backup > backend/data/test_import.sql

# Temporarily modify the script to use test_import.sql
# Then run the import
```

---

## Script Features

### Error Handling
- All scripts check prerequisites before running
- Clear error messages with suggested solutions
- Non-zero exit codes on failure for automation

### User Experience
- Colored output (green = success, red = error, yellow = warning)
- Progress indicators for long operations
- Interactive confirmations before destructive operations
- Helpful next steps after completion

### Safety
- Confirms before dropping databases
- Stops application before database operations
- Terminates active connections cleanly
- Can create backups before import (if enabled)

---

## Customization

### Change dump file location
Edit the `DUMP_FILE` variable in `import_to_docker.sh`:
```bash
DUMP_FILE="path/to/your/dump.sql"
```

### Add backup before import
Add this to `import_to_docker.sh` before the import:
```bash
# Create backup
BACKUP_FILE="backend/data/local_backup_$(date +%Y%m%d_%H%M%S).sql"
docker exec postgres_db pg_dump -U diakonx -d hostlib_db > "$BACKUP_FILE"
print_success "Backup created: $BACKUP_FILE"
```

### Use parallel restore (for Custom format)
In `import_to_docker.sh`, modify the `import_custom_dump` function:
```bash
docker exec postgres_db pg_restore -U diakonx -d hostlib_db \
  --no-owner --no-privileges -j 4 /tmp/dump.backup
```

### Skip Alembic migrations (if schema in dump)
Comment out the `run_alembic_migrations` call in `import_to_docker.sh`:
```bash
# run_alembic_migrations  # Skip if dump contains schema
```

---

## Troubleshooting

### "Permission denied" when running script
```bash
# Make executable
chmod +x scripts/*.sh

# Or run with bash explicitly
bash scripts/import_to_docker.sh
```

### "Docker is not running"
```bash
# Start Docker Desktop
# Wait for it to fully start, then verify:
docker info
```

### "Container not found"
```bash
# Start the container
docker compose up -d db_v2

# Wait for initialization
sleep 5
```

### Import fails with errors
1. Check the error message carefully
2. Ensure FastAPI is stopped: `docker-compose stop fastapi`
3. Check disk space: `df -h`
4. Review detailed guide: `docs/DATABASE_MIGRATION.md`

### Dump file seems corrupted
```bash
# Check file size
ls -lh backend/data/hostlib_backup

# Check first few lines
head -n 20 backend/data/hostlib_backup

# For Plain SQL, you should see SQL commands
# For Custom format, you'll see binary data
```

---

## Documentation

For more detailed information, see:
- **Quick Start:** [../docs/QUICK_START_MIGRATION.md](../docs/QUICK_START_MIGRATION.md)
- **Complete Guide:** [../docs/DATABASE_MIGRATION.md](../docs/DATABASE_MIGRATION.md)
- **Data Directory:** [../backend/data/README.md](../backend/data/README.md)
- **Setup Summary:** [../MIGRATION_SETUP_COMPLETE.md](../MIGRATION_SETUP_COMPLETE.md)

---

## Support

If you encounter issues:
1. Check the troubleshooting sections in the documentation
2. Review the script output for error messages
3. Enable debug mode: `bash -x scripts/script_name.sh`
4. Check Docker logs: `docker-compose logs db`

---

**Last Updated:** 2026-01-31
