from database import DatabaseManager

if __name__ == '__main__':
    db = DatabaseManager()
    print("Starting migration...")
    db.migrate_from_json()
    print("Done.")
