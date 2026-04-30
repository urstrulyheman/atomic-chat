# Database Migrations

Production schema changes should go through Alembic instead of `Base.metadata.create_all`.

Useful commands:

```bash
alembic upgrade head
alembic revision --autogenerate -m "describe change"
```

The app still calls `init_db()` in development so the MVP can run quickly with SQLite. For production, run migrations during deploy and disable startup schema creation.
