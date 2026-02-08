# Quick Start: Database Migration

This is a quick reference guide for migrating your database. For detailed instructions, see [DATABASE_MIGRATION.md](DATABASE_MIGRATION.md).

## TL;DR - I Just Want to Import My Dump

If you already have a dump file from PgAdmin:

```bash
# 1. Place your dump file here:
#    backend/data/hostlib_backup

# 2. Run the import script:
bash scripts/import_to_docker.sh

# 3. Start your application:
docker-compose up -d

# 4. Test it:
# Open http://localhost:8000/docs
```

That's it! ✅

---

## Method 1: Using PgAdmin Backup (Recommended)

### On the Server (PgAdmin):

1. Right-click database → **Backup**
2. Settings:
   - Format: **Plain** or **Custom**
   - Encoding: **UTF8**
   - Don't save: ✅ Owner, ✅ Privileges
3. Save and download the file

### On Your Local Machine:

```bash
# Place the dump file
cp /path/to/your/backup.sql backend/data/hostlib_backup

# Run import
bash scripts/import_to_docker.sh

# Start application
docker-compose up -d
```

---

## Method 2: Using Command Line Tools

### Complete automated process:

```bash
# This script handles everything
bash scripts/migrate_database.sh

# Follow the prompts:
# - Choose option 2 (create dump using pg_dump)
# - Enter server connection details
# - Confirm and wait
```

### Manual process:

```bash
# Step 1: Create dump from server
bash scripts/dump_from_server.sh

# Step 2: Import to Docker
bash scripts/import_to_docker.sh

# Step 3: Start application
docker-compose up -d
```

---

## What Each Script Does

| Script | Purpose | When to Use |
|--------|---------|-------------|
| `import_to_docker.sh` | Imports existing dump to Docker | You already have a dump file |
| `dump_from_server.sh` | Creates dump from remote server | You need to create a dump via command line |
| `migrate_database.sh` | Complete migration wizard | You want a guided process |

---

## Troubleshooting Quick Fixes

### Docker not running
```bash
# Start Docker Desktop, then:
docker-compose up -d db
```

### Import fails
```bash
# Stop application first:
docker-compose stop fastapi

# Then retry:
bash scripts/import_to_docker.sh
```

### Wrong file location
```bash
# File must be at:
backend/data/hostlib_backup
# Or:
backend/data/server_dump.sql
```

### Check if it worked
```bash
# Connect to database:
docker exec -it postgres_db psql -U diakonx -d hostlib_db

# In psql, run:
\dt                           # List tables
SELECT COUNT(*) FROM line;    # Count records
\q                            # Exit
```

---

## File Locations

```
HLViewer/
├── backend/data/
│   ├── hostlib_backup          # Place your dump file here
│   └── README.md               # Instructions for this directory
├── scripts/
│   ├── import_to_docker.sh     # Import dump to Docker
│   ├── dump_from_server.sh     # Create dump from server
│   └── migrate_database.sh     # Complete migration wizard
└── docs/
    ├── DATABASE_MIGRATION.md   # Detailed guide
    └── QUICK_START_MIGRATION.md # This file
```

---

## PgAdmin Settings Cheat Sheet

When creating backup in PgAdmin:

**Format Tab:**
- Format: `Plain` (small DB) or `Custom` (large DB)
- Encoding: `UTF8`

**Dump Options → Don't Save:**
- ✅ Owner
- ✅ Privilege
- ✅ Tablespace

**Dump Options → Queries:** (for Plain format only)
- ✅ Use Column Inserts
- ✅ Use Insert Commands

**Dump Options → Disable:**
- ✅ Trigger

---

## Common Commands

```bash
# Start database
docker-compose up -d db

# Stop application
docker-compose stop fastapi

# View logs
docker-compose logs -f fastapi

# Connect to database
docker exec -it postgres_db psql -U diakonx -d hostlib_db

# Check container status
docker ps

# Restart everything
docker-compose restart
```

---

## Need More Help?

- **Detailed guide:** [docs/DATABASE_MIGRATION.md](DATABASE_MIGRATION.md)
- **Data directory info:** [backend/data/README.md](../backend/data/README.md)
- **Run with verbose output:** Add `-x` to script: `bash -x scripts/import_to_docker.sh`

---

**Quick Tip:** The import script is safe - it will ask for confirmation before dropping the database, and shows detailed progress as it works.
