from app.workflows.checkpoint import setup_checkpoint_tables

if __name__ == "__main__":
    setup_checkpoint_tables()
    print("LangGraph PostgreSQL checkpoint tables are ready.")
