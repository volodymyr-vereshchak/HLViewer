#!/bin/bash

# Database Dump Script: Create backup from remote server
# This script creates a dump from a remote PostgreSQL server

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Output directory
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

# Function to check if pg_dump is available
check_pgdump() {
    if command -v pg_dump &> /dev/null; then
        PG_DUMP_CMD="pg_dump"
        print_success "Using local pg_dump"
        return 0
    elif docker ps --format '{{.Names}}' | grep -q "postgres_db"; then
        PG_DUMP_CMD="docker exec postgres_db pg_dump"
        print_success "Using pg_dump from Docker container"
        return 0
    else
        print_error "pg_dump not found!"
        print_info "Please install PostgreSQL client tools or start the postgres_db container"
        exit 1
    fi
}

# Function to get server connection details
get_connection_details() {
    print_header "Server Connection Details"

    read -p "Server host (e.g., 192.168.1.100): " SERVER_HOST
    read -p "Server port [5432]: " SERVER_PORT
    SERVER_PORT=${SERVER_PORT:-5432}

    read -p "Database name: " DB_NAME
    read -p "Username: " DB_USER
    read -sp "Password: " DB_PASSWORD
    echo ""

    echo ""
    print_info "Connection details:"
    echo "  Host: $SERVER_HOST"
    echo "  Port: $SERVER_PORT"
    echo "  Database: $DB_NAME"
    echo "  User: $DB_USER"
    echo ""

    read -p "Are these details correct? (y/n): " -n 1 -r
    echo ""

    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        print_info "Operation cancelled"
        exit 0
    fi
}

# Function to select dump format
select_dump_format() {
    print_header "Dump Format Selection"

    echo "Select dump format:"
    echo "  1) Plain SQL (text file, easy to inspect)"
    echo "  2) Custom (compressed binary, faster for large databases)"
    echo "  3) Data only (Plain SQL, excludes schema)"
    echo ""

    read -p "Choice [1]: " FORMAT_CHOICE
    FORMAT_CHOICE=${FORMAT_CHOICE:-1}

    case $FORMAT_CHOICE in
        1)
            DUMP_FORMAT="plain"
            FORMAT_FLAG=""
            print_success "Selected: Plain SQL format"
            ;;
        2)
            DUMP_FORMAT="custom"
            FORMAT_FLAG="-Fc"
            DUMP_FILE="${DUMP_FILE}.dump"
            print_success "Selected: Custom compressed format"
            ;;
        3)
            DUMP_FORMAT="data-only"
            FORMAT_FLAG="--data-only"
            print_success "Selected: Data-only Plain SQL format"
            ;;
        *)
            print_error "Invalid choice, using Plain SQL"
            DUMP_FORMAT="plain"
            FORMAT_FLAG=""
            ;;
    esac
}

# Function to test connection
test_connection() {
    print_info "Testing connection to server..."

    export PGPASSWORD="$DB_PASSWORD"

    if [[ $PG_DUMP_CMD == "pg_dump" ]]; then
        psql -h "$SERVER_HOST" -p "$SERVER_PORT" -U "$DB_USER" -d "$DB_NAME" \
            -c "SELECT version();" > /dev/null 2>&1
    else
        docker exec postgres_db psql -h "$SERVER_HOST" -p "$SERVER_PORT" \
            -U "$DB_USER" -d "$DB_NAME" -c "SELECT version();" > /dev/null 2>&1
    fi

    if [ $? -ne 0 ]; then
        print_error "Connection failed!"
        print_info "Please check your connection details and try again"
        print_info "Common issues:"
        echo "  - Firewall blocking connection"
        echo "  - Incorrect host/port"
        echo "  - Wrong username/password"
        echo "  - PostgreSQL not accepting remote connections"
        unset PGPASSWORD
        exit 1
    fi

    print_success "Connection successful"
}

# Function to create dump
create_dump() {
    print_header "Creating Database Dump"

    export PGPASSWORD="$DB_PASSWORD"

    print_info "This may take several minutes for large databases..."
    echo ""

    # Build pg_dump command
    local DUMP_CMD="$PG_DUMP_CMD -h $SERVER_HOST -p $SERVER_PORT -U $DB_USER -d $DB_NAME"
    DUMP_CMD="$DUMP_CMD --clean --no-owner --no-privileges $FORMAT_FLAG"

    # For plain format, add --inserts for compatibility
    if [[ $DUMP_FORMAT == "plain" || $DUMP_FORMAT == "data-only" ]]; then
        DUMP_CMD="$DUMP_CMD --inserts"
    fi

    # Execute dump
    if [[ $PG_DUMP_CMD == "pg_dump" ]]; then
        $DUMP_CMD -f "$DUMP_FILE" 2>&1 | while read line; do
            if [[ $line == *"ERROR"* ]]; then
                echo -e "${RED}$line${NC}"
            elif [[ $line == *"WARNING"* ]]; then
                echo -e "${YELLOW}$line${NC}"
            else
                echo "$line"
            fi
        done
    else
        $DUMP_CMD > "$DUMP_FILE" 2>&1
    fi

    unset PGPASSWORD

    if [ ${PIPESTATUS[0]} -ne 0 ]; then
        print_error "Dump creation failed!"
        rm -f "$DUMP_FILE"
        exit 1
    fi

    print_success "Dump created successfully!"
}

# Function to verify dump
verify_dump() {
    print_info "Verifying dump file..."

    if [ ! -f "$DUMP_FILE" ]; then
        print_error "Dump file not found!"
        exit 1
    fi

    local file_size=$(stat -c%s "$DUMP_FILE" 2>/dev/null || stat -f%z "$DUMP_FILE" 2>/dev/null)
    local size_mb=$((file_size / 1024 / 1024))

    if [ $file_size -eq 0 ]; then
        print_error "Dump file is empty!"
        rm -f "$DUMP_FILE"
        exit 1
    fi

    print_success "Dump file size: ${size_mb} MB"

    # Show first few lines for verification
    if [[ $DUMP_FORMAT == "plain" || $DUMP_FORMAT == "data-only" ]]; then
        echo ""
        print_info "First few lines of dump file:"
        head -n 10 "$DUMP_FILE" | while read line; do
            echo "  $line"
        done
    fi
}

# Main execution
main() {
    print_header "Database Dump: Create from Server"

    # Check prerequisites
    check_pgdump

    # Get connection details
    get_connection_details

    # Select dump format
    select_dump_format

    # Test connection
    test_connection

    # Create dump
    create_dump

    # Verify dump
    echo ""
    verify_dump

    # Success message
    print_header "Dump Created Successfully!"

    echo "Dump file location: $DUMP_FILE"
    echo ""
    echo "Next steps:"
    echo "  1. Review the dump file (if needed)"
    echo "  2. Import to Docker: bash scripts/import_to_docker.sh"
    echo ""
}

# Run main function
main
