#!/usr/bin/env python3
"""
Test runner script for HLViewer project.

This script provides convenient commands to run different types of tests.
"""

import sys
import subprocess
import argparse
from pathlib import Path


def run_command(cmd, description):
    """Run a command and handle errors."""
    print(f"\n{'='*60}")
    print(f"Running: {description}")
    print(f"Command: {' '.join(cmd)}")
    print(f"{'='*60}\n")
    
    try:
        result = subprocess.run(cmd, check=True, capture_output=False)
        print(f"\n✅ {description} completed successfully!")
        return True
    except subprocess.CalledProcessError as e:
        print(f"\n❌ {description} failed with exit code {e.returncode}")
        return False
    except FileNotFoundError:
        print(f"\n❌ Command not found: {cmd[0]}")
        print("Make sure pytest is installed: pip install -r requirements.txt")
        return False


def main():
    parser = argparse.ArgumentParser(description="HLViewer Test Runner")
    parser.add_argument(
        "test_type",
        choices=["all", "unit", "integration", "api", "database", "coverage"],
        help="Type of tests to run"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose output"
    )
    parser.add_argument(
        "--no-cov",
        action="store_true",
        help="Disable coverage reporting"
    )
    parser.add_argument(
        "--markers",
        action="store_true",
        help="Show available test markers"
    )
    
    args = parser.parse_args()
    
    # Base pytest command
    base_cmd = ["python", "-m", "pytest"]
    
    if args.verbose:
        base_cmd.append("-v")
    
    if not args.no_cov:
        base_cmd.extend(["--cov=backend", "--cov=utils", "--cov-report=term-missing"])
    
    # Test type specific commands
    if args.test_type == "all":
        cmd = base_cmd + ["tests/"]
    elif args.test_type == "unit":
        cmd = base_cmd + ["tests/unit/"]
    elif args.test_type == "integration":
        cmd = base_cmd + ["tests/integration/"]
    elif args.test_type == "api":
        cmd = base_cmd + ["tests/integration/test_api_endpoints.py"]
    elif args.test_type == "database":
        cmd = base_cmd + ["tests/integration/test_database_integration.py", "-m", "database"]
    elif args.test_type == "coverage":
        cmd = ["python", "-m", "pytest", "--cov=backend", "--cov=utils", 
               "--cov-report=html:htmlcov", "--cov-report=xml", "tests/"]
    
    if args.markers:
        cmd = ["python", "-m", "pytest", "--markers"]
    
    # Run the command
    success = run_command(cmd, f"{args.test_type.title()} tests")
    
    if success:
        print("\n🎉 All tests passed!")
        sys.exit(0)
    else:
        print("\n💥 Some tests failed!")
        sys.exit(1)


if __name__ == "__main__":
    main() 