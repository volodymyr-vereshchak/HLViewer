#!/bin/bash

# Database Migration Script: Import to Docker PostgreSQL
# This script imports a database dump into the local Docker PostgreSQL container

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
CONTAINER_NAME="postgres_db"
DB_NAME="hostlib_db"
DB_USER="diakonx"
DUMP_DIR="backend/data"
DUMP_FILE="${DUMP_DIR}/hostlib_backup"

# Function to print colored messages
print_success() {
    echo -e "${GREEN}[✓]${NC} $1"
}

print_error() {
    echo -e "${RED}[✗]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[!]${NC} $1"
}

print_info() {
    echo -e "${BLUE}[→]${NC} $1"
}

print_header() {
    echo ""
    echo "========================================"
    echo "  $1"
    echo "========================================"
    echo ""
}

# Function to check if Docker is running
check_docker() {
    if ! docker info > /dev/null 2>&1; then
        print_error "Docker is not running!"
        print_info "Please start Docker Desktop and try again."
        exit 1
    fi
}

# Function to check if container is running
check_container() {
    if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        print_error "Container '${CONTAINER_NAME}' is not running!"
        print_info "Starting the database container..."
        docker-compose up -d db
        sleep 5

        if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
            print_error "Failed to start container. Please run: docker-compose up -d db"
            exit 1
        fi
        print_success "Container started successfully"
    fi
}

# Function to check if dump file exists
check_dump_file() {
    if [ ! -f "$DUMP_FILE" ]; then
        print_error "Dump file not found: $DUMP_FILE"
        print_info "Please place your database dump at: $DUMP_FILE"
        print_info "You can create it using PgAdmin or pg_dump"
        exit 1
    fi

    local file_size=$(stat -c%s "$DUMP_FILE" 2>/dev/null || stat -f%z "$DUMP_FILE" 2>/dev/null)
    local size_mb=$((file_size / 1024 / 1024))
    print_success "Dump file found (${size_mb} MB)"
}

# Function to determine dump format
determine_dump_format() {
    local first_bytes=$(head -c 100 "$DUMP_FILE")

    if echo "$first_bytes" | grep -q "INSERT\|COPY\|CREATE"; then
        echo "plain"
    else
        echo "custom"
    fi
}

# Function to stop FastAPI application
stop_fastapi() {
    if docker ps --format '{{.Names}}' | grep -q "fastapi_app"; then
        print_info "Stopping FastAPI application..."
        docker-compose stop fastapi
        print_success "FastAPI stopped"
    fi
}

# Function to terminate active connections
terminate_connections() {
    print_info "Terminating active database connections..."
    docker exec -i ${CONTAINER_NAME} psql -U ${DB_USER} -d postgres -c \
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='${DB_NAME}' AND pid <> pg_backend_pid();" \
        > /dev/null 2>&1 || true
    print_success "Connections terminated"
}

# Function to recreate database
recreate_database() {
    print_info "Dropping database '${DB_NAME}'..."
    docker exec -i ${CONTAINER_NAME} psql -U ${DB_USER} -d postgres -c \
        "DROP DATABASE IF EXISTS ${DB_NAME};" > /dev/null 2>&1

    if [ $? -ne 0 ]; then
        print_error "Failed to drop database"
        exit 1
    fi
    print_success "Database dropped"

    print_info "Creating database '${DB_NAME}'..."
    docker exec -i ${CONTAINER_NAME} psql -U ${DB_USER} -d postgres -c \
        "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER} ENCODING 'UTF8';" > /dev/null 2>&1

    if [ $? -ne 0 ]; then
        print_error "Failed to create database"
        exit 1
    fi
    print_success "Database created"
}

# Function to run Alembic migrations to create schema
run_alembic_migrations() {
    print_info "Running Alembic migrations to create schema..."

    # Check if fastapi container exists
    if docker ps -a --format '{{.Names}}' | grep -q "fastapi_app"; then
        # Run migrations through the fastapi container
        docker exec fastapi_app sh -c "cd backend && alembic upgrade head" > /dev/null 2>&1

        if [ $? -eq 0 ]; then
            print_success "Schema created via Alembic migrations"
            return 0
        else
            print_warning "Alembic migrations failed or not configured"
            return 1
        fi
    else
        print_warning "FastAPI container not available for Alembic migrations"
        return 1
    fi
}

# Function to import plain SQL dump
import_plain_dump() {
    print_info "Importing Plain SQL dump (this may take several minutes)..."

    docker exec -i ${CONTAINER_NAME} psql -U ${DB_USER} -d ${DB_NAME} < "$DUMP_FILE" 2>&1 | \
        grep -v "^INSERT" | grep -v "^COPY" | grep -v "^SET" | \
        while read line; do
            if [[ $line == *"ERROR"* ]]; then
                echo -e "${RED}$line${NC}"
            elif [[ $line == *"WARNING"* ]]; then
                echo -e "${YELLOW}$line${NC}"
            fi
        done

    if [ ${PIPESTATUS[0]} -ne 0 ]; then
        print_error "Import failed!"
        print_info "Check the error messages above for details"
        exit 1
    fi

    print_success "Import completed!"
}

# Function to import custom dump
import_custom_dump() {
    print_info "Importing Custom format dump..."

    # Copy dump file to container
    docker cp "$DUMP_FILE" ${CONTAINER_NAME}:/tmp/dump.backup

    # Restore using pg_restore
    docker exec ${CONTAINER_NAME} pg_restore -U ${DB_USER} -d ${DB_NAME} \
        --no-owner --no-privileges --verbose /tmp/dump.backup 2>&1 | \
        grep -v "^pg_restore: " | \
        while read line; do
            if [[ $line == *"ERROR"* ]]; then
                echo -e "${RED}$line${NC}"
            elif [[ $line == *"WARNING"* ]]; then
                echo -e "${YELLOW}$line${NC}"
            fi
        done

    # Clean up
    docker exec ${CONTAINER_NAME} rm /tmp/dump.backup

    if [ ${PIPESTATUS[0]} -ne 0 ]; then
        print_error "Import failed!"
        exit 1
    fi

    print_success "Import completed!"
}

# Function to verify import
verify_import() {
    print_info "Verifying import..."
    echo ""

    # Count tables
    local table_count=$(docker exec ${CONTAINER_NAME} psql -U ${DB_USER} -d ${DB_NAME} \
        -t -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public';" 2>/dev/null | tr -d ' ')
    print_success "Tables imported: ${table_count}"

    # Check key tables
    local tables=("line" "hourly_archive" "daily_archive")
    for table in "${tables[@]}"; do
        local count=$(docker exec ${CONTAINER_NAME} psql -U ${DB_USER} -d ${DB_NAME} \
            -t -c "SELECT COUNT(*) FROM ${table};" 2>/dev/null | tr -d ' ')

        if [ -n "$count" ]; then
            # Format number with thousands separator
            printf "${GREEN}[✓]${NC} Records in '${table}': %'d\n" $count
        else
            print_warning "Table '${table}' not found or empty"
        fi
    done
}

# Main execution
main() {
    print_header "Database Migration: Import to Docker"

    print_info "Checking prerequisites..."
    check_docker
    check_container
    check_dump_file

    # Determine dump format
    DUMP_FORMAT=$(determine_dump_format)
    print_success "Dump format: ${DUMP_FORMAT^^}"

    # Warning before destructive operation
    echo ""
    print_warning "This will DROP and recreate the database '${DB_NAME}'"
    read -p "Continue? (y/n): " -n 1 -r
    echo ""

    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        print_info "Operation cancelled by user"
        exit 0
    fi

    echo ""

    # Stop FastAPI
    stop_fastapi

    # Terminate connections
    terminate_connections

    # Recreate database
    recreate_database

    # Run Alembic migrations to create schema first
    # This is important since the dump contains only data
    run_alembic_migrations

    # Import dump based on format
    if [ "$DUMP_FORMAT" = "plain" ]; then
        import_plain_dump
    else
        import_custom_dump
    fi

    # Verify
    echo ""
    verify_import

    # Success message
    print_header "Migration completed successfully!"

    echo "Next steps:"
    echo "  1. Start the application: docker-compose up -d"
    echo "  2. Check logs: docker-compose logs -f fastapi"
    echo "  3. Test API: http://localhost:8000/docs"
    echo ""
}

# Run main function
main
