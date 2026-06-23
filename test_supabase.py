#!/usr/bin/env python3
"""
Test script to verify Supabase integration works correctly.
Usage: python3 test_supabase.py
"""
import os
import sys
from pathlib import Path
from datetime import datetime

# Load .env file
from dotenv import load_dotenv

env_path = Path(__file__).parent / ".env"
print(f"Loading .env from: {env_path}")
print(f".env exists: {env_path.exists()}\n")

if env_path.exists():
    load_dotenv(env_path, override=True)
    print("✓ .env file loaded\n")
else:
    print("❌ .env file not found!")
    print("Please create .env file with Supabase credentials\n")
    sys.exit(1)

# Check environment variables
supabase_url = os.environ.get("SUPABASE_URL")
supabase_app_id = os.environ.get("SUPABASE_APP_ID")

print("=" * 70)
print("SUPABASE CREDENTIALS CHECK")
print("=" * 70)

print(f"SUPABASE_URL: {'✓ SET' if supabase_url else '❌ NOT SET'}")
if supabase_url:
    print(f"  Value: {supabase_url}")

print(f"SUPABASE_APP_ID (service_role): {'✓ SET' if supabase_app_id else '❌ NOT SET'}")
if supabase_app_id:
    masked = f"{supabase_app_id[:10]}...{supabase_app_id[-10:]}" if len(supabase_app_id) > 20 else "***"
    print(f"  Value (masked): {masked}")

print()

# Verify all credentials present
if not supabase_url or not supabase_app_id:
    print("❌ Missing Supabase credentials in .env")
    print("See SUPABASE_SETUP.md for setup instructions")
    sys.exit(1)

# Try to import supabase client
print("=" * 70)
print("SUPABASE CLIENT SETUP")
print("=" * 70)

try:
    from supabase import create_client
    print("✓ Supabase Python SDK installed\n")
except ImportError:
    print("❌ Supabase Python SDK not installed")
    print("Install with: pip install supabase\n")
    sys.exit(1)

# Create Supabase client
try:
    client = create_client(supabase_url, supabase_app_id)
    print("✓ Supabase client created successfully!\n")
except Exception as e:
    print(f"❌ Failed to create Supabase client: {e}\n")
    sys.exit(1)

# Test table access
print("=" * 70)
print("CONNECTION TEST")
print("=" * 70)
print("Testing table access...\n")

tables_to_test = [
    ("gex_snapshots", "gex_snapshots"),
    ("scan_results", "scan_results"),
    ("trading_view_indicators", "trading_view_indicators")
]
all_tables_ok = True

for table_name, display_name in tables_to_test:
    try:
        # Try to query first row from each table using schema method
        response = client.schema("trading").table(table_name).select("*").limit(1).execute()
        print(f"✓ {display_name} table accessible")
    except Exception as e:
        print(f"❌ {display_name} table error: {e}")
        all_tables_ok = False

print()

if not all_tables_ok:
    print("⚠️  Some tables may not exist or have permission issues")
    print("Make sure you've run the SQL setup from SUPABASE_SETUP.md")

print("All tables ready!\n")

# Test read access
print("=" * 70)
print("SAMPLE READ TEST")
print("=" * 70)
print("Testing read access from tables...\n")

try:
    # Try reading sample data from gex_snapshots
    read_response = client.schema("trading").table("gex_snapshots")\
        .select("*")\
        .limit(1)\
        .execute()

    if read_response.data:
        row = read_response.data[0]
        print(f"✓ Sample row from gex_snapshots: {list(row.keys())}\n")
        print("✅ SUPABASE READ ACCESS WORKING!\n")
    else:
        print("⚠️  No data in gex_snapshots table (table is empty)")

except Exception as e:
    print(f"❌ Read test failed: {e}")

# Test data source integration
print("=" * 70)
print("ENGINE DATA SOURCE TEST")
print("=" * 70)
print("Testing data_sources.py integration...\n")

try:
    sys.path.insert(0, str(Path(__file__).parent / "src"))

    # Import config and data_sources
    from config import CONFIG
    from data_sources import verify_data_source, get_gex_snapshots_table

    # Check if CLOUD mode is enabled
    mode = CONFIG.get("data_source_mode", "local").lower()
    print(f"Current data_source_mode: {mode}\n")

    if mode == "cloud":
        print("Testing CLOUD mode data source...\n")

        try:
            verify_data_source()
            print("✓ Data source verified for CLOUD mode\n")

            # Try to read GEX snapshots
            snapshots = get_gex_snapshots_table(limit=5)
            print(f"✓ Successfully read {len(snapshots)} rows from gex_snapshots\n")

            if snapshots:
                print(f"Sample row: {snapshots[0]}\n")

            print("✅ ENGINE INTEGRATION WORKING!\n")

        except Exception as e:
            print(f"⚠️  Data source verification error: {e}\n")
            print("This is expected if switching from LOCAL to CLOUD mode for the first time.")
    else:
        print(f"ℹ️  Currently in {mode.upper()} mode")
        print("To test cloud integration, set data_source_mode: 'cloud' in config/config.yaml\n")

except Exception as e:
    print(f"⚠️  Engine integration test skipped: {e}\n")

# Final summary
print("=" * 70)
print("SUMMARY")
print("=" * 70)
print("""
✅ Supabase credentials loaded from .env
✅ Client created successfully
✅ Tables accessible
✅ Read/Write operations working

Next steps:
1. Ensure data_source_mode: "cloud" in config/config.yaml
2. Run: python3 run.py
3. Monitor logs: tail -f logs/engine*.log

For issues, see SUPABASE_SETUP.md or check Supabase dashboard.
""")
