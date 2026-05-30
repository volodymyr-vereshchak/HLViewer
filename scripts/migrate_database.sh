#!/bin/bash

# Complete Database Migration Script
# This script handles the full migration process from server to local Docker

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

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
    echo "=========================================="
    echo "  $1"
    echo "=========================================="
    echo ""
}

# Function to select migration method
select_method() {
    print_header "Complete Database Migration"

    echo "This script will help you migrate your database from the server to local Docker."
    echo ""
    echo "Select migration method:"
    echo ""
    echo "  1) I already have a dump file (created via PgAdmin)"
    echo "  2) Create dump using pg_dump from command line"
    echo "  3) Exit"
    echo ""

    read -p "Choice [1]: " METHOD_CHOICE
    METHOD_CHOICE=${METHOD_CHOICE:-1}

    case $METHOD_CHOICE in
        1)
            METHOD="existing"
            ;;
        2)
            METHOD="pgdump"
            ;;
        3)
            print_info "Operation cancelled"
            exit 0
            ;;
        *)
            print_error "Invalid choice"
            exit 1
            ;;
    esac
}

# Function to check for existing dump
check_existing_dump() {
    local dump_files=$(find backend/data -maxdepth 1 \( -name "hostlib_backup" -o -name "server_dump.*" -o -name "*.dump" \) 2>/dev/null)

    if [ -z "$dump_files" ]; then
        print_warning "No dump file found in backend/data/"
        print_info "Please place your dump file in backend/data/ with one of these names:"
        echo "  - hostlib_backup"
        echo "  - server_dump.sql"
        echo "  - server_dump.dump"
        echo ""
        read -p "Would you like to create a dump using pg_dump instead? (y/n): " -n 1 -r
        echo ""

        if [[ $REPLY =~ ^[Yy]$ ]]; then
            METHOD="pgdump"
        else
            print_info "Operation cancelled"
            exit 0
        fi
    else
        print_success "Found dump file(s):"
        echo "$dump_files" | while read file; do
            local size=$(stat -c%s "$file" 2>/dev/null || stat -f%z "$file" 2>/dev/null)
            local size_mb=$((size / 1024 / 1024))
            echo "  - $file (${size_mb} MB)"
        done
        echo ""
    fi
}

# Function to run migration steps
run_migration() {
    print_header "Migration Steps"

    # Step 1: Get dump file
    if [ "$METHOD" = "pgdump" ]; then
        print_info "Step 1: Creating dump from server..."
        bash scripts/dump_from_server.sh

        if [ $? -ne 0 ]; then
            print_error "Dump creation failed!"
            exit 1
        fi
    else
        print_success "Step 1: Using existing dump file"
    fi

    # Step 2: Import to Docker
    print_info "Step 2: Importing to local Docker..."
    echo ""

    bash scripts/import_to_docker.sh

    if [ $? -ne 0 ]; then
        print_error "Import failed!"
        exit 1
    fi
}

# Function to show final summary
show_summary() {
    print_header "Migration Complete!"

    echo "Your database has been successfully migrated to local Docker."
    echo ""
    echo "What was done:"
    echo "  ✓ Database dump created (or used existing)"
    echo "  ✓ Local database recreated"
    echo "  ✓ Data imported"
    echo "  ✓ Import verified"
    echo ""
    echo "Next steps:"
    echo "  1. Start your application:"
    echo "     docker-compose up -d"
    echo ""
    echo "  2. Verify the application works:"
    echo "     http://localhost:8001/docs"
    echo ""
    echo "  3. Check the logs:"
    echo "     docker compose logs -f fastapi_v2"
    echo ""
    echo "Troubleshooting:"
    echo "  - If you encounter issues, check: docs/DATABASE_MIGRATION.md"
    echo "  - View import logs: they should be displayed above"
    echo "  - Connect to database: docker exec -it postgres_db_v2 psql -U diakonx -d hostlib_db_v2"
    echo ""

    print_success "Migration completed successfully!"
}

# Main execution
main() {
    # Select method
    select_method

    # If using existing dump, check if it exists
    if [ "$METHOD" = "existing" ]; then
        check_existing_dump
    fi

    # Confirm before proceeding
    echo ""
    print_warning "This will DROP and recreate your local database!"
    read -p "Continue with migration? (y/n): " -n 1 -r
    echo ""

    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        print_info "Operation cancelled"
        exit 0
    fi

    # Run migration
    run_migration

    # Show summary
    echo ""
    show_summary
}

# Run main function
main
