# Database Migration Guide

This guide explains how to migrate your PostgreSQL database from a remote server to your local Docker environment.

## Table of Contents

1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Method 1: Using PgAdmin (Recommended)](#method-1-using-pgadmin-recommended)
4. [Method 2: Using Command Line Tools](#method-2-using-command-line-tools)
5. [Verification](#verification)
6. [Troubleshooting](#troubleshooting)
7. [Advanced Options](#advanced-options)

---

## Overview

The migration process involves two main steps:
1. **Creating a database dump** from the server
2. **Importing the dump** into your local Docker PostgreSQL container

The project includes an automated import script that handles the second step.

---

## Prerequisites

Before starting, ensure you have:

- ✅ **Docker Desktop** running
- ✅ **PostgreSQL container** running (`postgres_db`)
- ✅ **Access to the server database** (via PgAdmin or direct connection)
- ✅ **Sufficient disk space** (database dumps can be 1GB+ in size)

**Check your setup:**
```bash
# Verify Docker is running
docker info

# Verify PostgreSQL container is running
docker ps | grep postgres_db

# Start the container if needed
docker-compose up -d db
```

---

## Method 1: Using PgAdmin (Recommended)

This is the easiest and most reliable method.

### Step 1: Create Backup in PgAdmin

1. **Open PgAdmin** and connect to your server database

2. **Navigate to your database** in the Object Explorer
   - Servers → Your Server → Databases → (your database name)

3. **Right-click on the database** → Select **"Backup..."**

4. **Configure General Tab:**
   - **Filename:** `hostlib_backup` or `server_dump.sql`
   - **Format:**
     - **Plain** - SQL text file (recommended for small databases < 500MB)
     - **Custom** - Compressed binary (recommended for large databases)
   - **Encoding:** UTF8
   - **Role name:** Leave empty
   - **Number of jobs:** 1 (or higher for Custom format on large databases)

5. **Configure Dump Options Tab:**

   **Section: Don't Save**
   - ✅ **Owner** - Don't save object owners
   - ✅ **Privilege** - Don't save access privileges
   - ✅ **Tablespace** - Don't save tablespace assignments

   **Section: Queries** (for Plain format only)
   - ✅ **Use Column Inserts** - Use column names in INSERT statements
   - ✅ **Use Insert Commands** - Use INSERT instead of COPY (slower but more compatible)

   **Section: Disable**
   - ✅ **Trigger** - Disable triggers during import (recommended)

6. **Configure Data/Objects Tab:**
   - ✅ **Blobs** - Include large objects (if applicable)
   - ✅ **Data** - Include table data
   - ✅ **Pre-data** - Include schema (tables, sequences, etc.)
   - ✅ **Post-data** - Include constraints and triggers

   **Alternative for data-only dump:**
   - ⬜ **Pre-data** - Uncheck (schema will be created by Alembic)
   - ✅ **Data** - Check
   - ⬜ **Post-data** - Uncheck

7. **Click "Backup"** and wait for completion
   - For large databases, this may take several minutes
   - PgAdmin will show a progress dialog

8. **Download the file** to your local machine
   - Save it as: `D:\Projects\HLViewer\HLViewer\backend\data\hostlib_backup`

### Step 2: Import to Local Docker

1. **Place the dump file** in the correct location:
   ```
   D:\Projects\HLViewer\HLViewer\backend\data\hostlib_backup
   ```

2. **Run the import script:**
   ```bash
   bash scripts/import_to_docker.sh
   ```

3. **Follow the prompts:**
   - The script will detect the dump format automatically
   - Confirm when asked to proceed (this will DROP and recreate the database)
   - Wait for the import to complete (may take several minutes for large databases)

4. **Check the verification output:**
   - Number of tables imported
   - Record counts for key tables
   - Any warnings or errors

---

## Method 2: Using Command Line Tools

If you don't have access to PgAdmin GUI, you can use `pg_dump` from the command line.

### Step 1: Create Dump Using pg_dump

**Option A: Using local pg_dump (if installed)**

```bash
# Set password as environment variable to avoid prompts
export PGPASSWORD='your_password'

# Create the dump
pg_dump -h server_host -p 5432 -U username -d database_name \
  --clean \
  --inserts \
  --no-owner \
  --no-privileges \
  --encoding UTF8 \
  -f backend/data/hostlib_backup

# Clear password from environment
unset PGPASSWORD
```

**Option B: Using pg_dump from Docker container**

```bash
# Run pg_dump through the local Docker container
docker exec postgres_db pg_dump \
  -h server_host \
  -p 5432 \
  -U username \
  -d database_name \
  --clean \
  --inserts \
  --no-owner \
  --no-privileges \
  > backend/data/hostlib_backup
```

**Important flags:**
- `--clean` - Add DROP commands before CREATE
- `--inserts` - Use INSERT statements (slower but more compatible)
- `--no-owner` - Don't restore ownership information
- `--no-privileges` - Don't restore access privileges
- `--encoding UTF8` - Use UTF-8 encoding for the dump

**For data-only dump:**
```bash
pg_dump -h server_host -U username -d database_name \
  --data-only \
  --inserts \
  --no-owner \
  --no-privileges \
  -f backend/data/hostlib_backup
```

### Step 2: Import to Local Docker

Same as Method 1, Step 2 - run `bash scripts/import_to_docker.sh`

---

## Verification

After the import completes, verify everything is working:

### 1. Check Database Connection

```bash
# Connect to the database
docker exec -it postgres_db psql -U diakonx -d hostlib_db

# List all tables
\dt

# Check record counts
SELECT COUNT(*) FROM line;
SELECT COUNT(*) FROM hourly_archive;
SELECT COUNT(*) FROM daily_archive;

# Exit psql
\q
```

### 2. Start the Application

```bash
# Start all services
docker-compose up -d

# Check logs for errors
docker-compose logs -f fastapi

# Press Ctrl+C to stop following logs
```

### 3. Test the API

1. Open your browser to: http://localhost:8000/docs
2. Try a few API endpoints:
   - `GET /api/v1/lines` - List all lines
   - `GET /api/v1/hourly_archive` - Get hourly data
3. Verify the data looks correct

### 4. Check Alembic Migration Status

```bash
# Check current migration version
docker exec fastapi_app sh -c "cd backend && alembic current"

# Should show the latest migration
# Example output: 089898b167d1 (head)
```

---

## Troubleshooting

### Problem: "Docker is not running"

**Solution:**
```bash
# Start Docker Desktop application
# Then verify:
docker info
```

### Problem: "Container postgres_db is not running"

**Solution:**
```bash
# Start the database container
docker-compose up -d db

# Wait a few seconds, then verify
docker ps | grep postgres_db
```

### Problem: "Dump file not found"

**Solution:**
- Verify the file exists: `ls -lh backend/data/hostlib_backup`
- Check the filename matches exactly (no extra extensions)
- Ensure the file is in the correct directory

### Problem: "Permission denied" when creating backup in PgAdmin

**Solution:**
- Ask your database administrator to grant SELECT permissions:
  ```sql
  GRANT SELECT ON ALL TABLES IN SCHEMA public TO your_username;
  GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO your_username;
  ```

### Problem: Import is very slow

**Solutions:**
1. **For Plain format dumps:**
   - Recreate dump without `--inserts` flag (use COPY instead)
   - Use Custom format instead

2. **For Custom format dumps:**
   - Use parallel restore: `pg_restore -j 4` (4 jobs)
   - Modify the import script to add `-j` flag

3. **General optimizations:**
   - Temporarily disable triggers and constraints
   - Create indexes after data import
   - Increase Docker memory allocation

### Problem: "database is being accessed by other users"

**Solution:**
```bash
# Stop the FastAPI application
docker-compose stop fastapi

# Terminate all connections manually
docker exec postgres_db psql -U diakonx -d postgres -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='hostlib_db';"

# Then run the import script again
bash scripts/import_to_docker.sh
```

### Problem: Foreign key constraint errors during import

**Solutions:**
1. **Ensure PgAdmin "Disable Trigger" option is checked**
2. **Or temporarily disable foreign keys:**
   ```bash
   # Before import
   docker exec postgres_db psql -U diakonx -d hostlib_db -c \
     "SET session_replication_role = 'replica';"

   # Run import
   # ...

   # After import
   docker exec postgres_db psql -U diakonx -d hostlib_db -c \
     "SET session_replication_role = 'origin';"
   ```

### Problem: "role does not exist" warnings

**This is normal!** These warnings appear when:
- The dump was created with `--no-owner` flag
- PostgreSQL can't find the original database owner
- Your local database uses different user names

**Solution:** Ignore these warnings - they don't affect functionality.

### Problem: Character encoding errors (corrupted Cyrillic text)

**Solutions:**
1. **Verify database encoding:**
   ```bash
   docker exec postgres_db psql -U diakonx -d hostlib_db -c \
     "SHOW SERVER_ENCODING;"
   # Should show: UTF8
   ```

2. **Recreate database with explicit encoding:**
   ```bash
   docker exec postgres_db psql -U diakonx -d postgres -c \
     "DROP DATABASE hostlib_db;"
   docker exec postgres_db psql -U diakonx -d postgres -c \
     "CREATE DATABASE hostlib_db ENCODING 'UTF8' LC_COLLATE='en_US.UTF-8' LC_CTYPE='en_US.UTF-8';"
   ```

3. **Ensure dump was created with UTF8 encoding** in PgAdmin settings

### Problem: Out of memory during import

**Solutions:**
1. **Increase Docker memory limit:**
   - Docker Desktop → Settings → Resources → Memory
   - Increase to at least 4GB (8GB recommended for large databases)

2. **Import in smaller chunks** (for Plain format):
   ```bash
   # Split the dump file
   split -l 100000 backend/data/hostlib_backup backend/data/chunk_

   # Import each chunk
   for file in backend/data/chunk_*; do
     docker exec -i postgres_db psql -U diakonx -d hostlib_db < "$file"
   done
   ```

### Problem: Alembic version conflicts

**Solution:**
```bash
# Check current Alembic version
docker exec fastapi_app sh -c "cd backend && alembic current"

# If it shows a different version than expected, manually set it
docker exec postgres_db psql -U diakonx -d hostlib_db -c \
  "UPDATE alembic_version SET version_num='089898b167d1';"

# Verify
docker exec fastapi_app sh -c "cd backend && alembic current"
```

### Problem: Import script fails with "command not found"

**Solution for Windows/Git Bash:**
```bash
# Make sure you're using bash, not sh
bash scripts/import_to_docker.sh

# Make the script executable
chmod +x scripts/import_to_docker.sh

# Run it
./scripts/import_to_docker.sh
```

---

## Advanced Options

### Creating a Backup Before Import

To save your current local database before importing:

```bash
# Create a backup of current local database
docker exec postgres_db pg_dump -U diakonx -d hostlib_db \
  > backend/data/local_backup_$(date +%Y%m%d_%H%M%S).sql

# This creates a timestamped backup file
```

### Importing Only Specific Tables

```bash
# For Plain format dumps, you can filter tables
grep -A 1000 "CREATE TABLE your_table" backend/data/hostlib_backup | \
  docker exec -i postgres_db psql -U diakonx -d hostlib_db

# For Custom format dumps, use pg_restore with --table flag
docker exec postgres_db pg_restore -U diakonx -d hostlib_db \
  --table=your_table /tmp/dump.backup
```

### Parallel Restore for Large Databases

For Custom format dumps only:

```bash
# Copy dump to container
docker cp backend/data/hostlib_backup postgres_db:/tmp/dump.backup

# Restore with 4 parallel jobs
docker exec postgres_db pg_restore -U diakonx -d hostlib_db \
  --no-owner --no-privileges \
  -j 4 \
  /tmp/dump.backup
```

### Comparing Server and Local Databases

```bash
# Count tables
echo "Server tables:"
PGPASSWORD='password' psql -h server_host -U username -d database_name \
  -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public';"

echo "Local tables:"
docker exec postgres_db psql -U diakonx -d hostlib_db \
  -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public';"

# Compare record counts
for table in line hourly_archive daily_archive; do
  echo "Table: $table"
  echo -n "  Server: "
  PGPASSWORD='password' psql -h server_host -U username -d database_name \
    -t -c "SELECT COUNT(*) FROM $table;"
  echo -n "  Local: "
  docker exec postgres_db psql -U diakonx -d hostlib_db \
    -t -c "SELECT COUNT(*) FROM $table;"
done
```

---

## Best Practices

1. **Always backup before major operations**
   - Create a local backup before importing new data
   - Keep server dumps in a safe location

2. **Use appropriate dump format:**
   - **Plain format** for small databases (< 500MB) or when you need to inspect/edit
   - **Custom format** for large databases (better compression, faster restore)

3. **Verify checksums** for critical data:
   ```bash
   # Calculate checksum of dump file
   sha256sum backend/data/hostlib_backup
   ```

4. **Document your migrations:**
   - Keep notes on when you migrated
   - Record any issues encountered
   - Track database versions

5. **Regular migrations:**
   - Set up a schedule for regular database syncs
   - Automate the process if possible

6. **Test after migration:**
   - Always verify key functionality after importing
   - Check critical tables and relationships
   - Run your test suite

---

## Additional Resources

- **PostgreSQL Documentation:** https://www.postgresql.org/docs/
- **pg_dump Manual:** https://www.postgresql.org/docs/current/app-pgdump.html
- **pg_restore Manual:** https://www.postgresql.org/docs/current/app-pgrestore.html
- **PgAdmin Documentation:** https://www.pgadmin.org/docs/

---

## Getting Help

If you encounter issues not covered in this guide:

1. Check the application logs:
   ```bash
   docker-compose logs -f fastapi
   docker-compose logs -f db
   ```

2. Check PostgreSQL logs:
   ```bash
   docker exec postgres_db cat /var/log/postgresql/postgresql-*.log
   ```

3. Enable verbose output in the import script (edit the script and add `-v` flags)

4. Consult the project's issue tracker or contact the development team

---

**Last Updated:** 2026-01-31
