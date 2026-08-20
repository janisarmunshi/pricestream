from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('ticks', '0001_initial'),
    ]

    operations = [
        migrations.RunSQL(
            sql="CREATE EXTENSION IF NOT EXISTS timescaledb;",
            reverse_sql=migrations.RunSQL.noop,
        ),
        # Timescale requires every unique index on a hypertable to include the
        # partitioning column. Django's BigAutoField PK is a unique index on `id`
        # alone, so it must be dropped before create_hypertable — Tick rows don't
        # need individual identity beyond (account_id, token, time) anyway, and the
        # model's own indexes already cover the query patterns.
        migrations.RunSQL(
            sql="ALTER TABLE ticks_tick DROP CONSTRAINT ticks_tick_pkey;",
            reverse_sql="ALTER TABLE ticks_tick ADD CONSTRAINT ticks_tick_pkey PRIMARY KEY (id);",
        ),
        migrations.RunSQL(
            sql="SELECT create_hypertable('ticks_tick', 'time', migrate_data => true, if_not_exists => true);",
            reverse_sql=migrations.RunSQL.noop,
        ),
        # Compression + retention map onto Timescale's own policies rather than a
        # hand-rolled scheme (see streaming.StreamingConfig for the tunable knobs
        # this should eventually drive; hardcoded defaults here, adjustable later
        # via `SELECT add_compression_policy(...)` / `add_retention_policy(...)`
        # with different intervals — this migration only establishes the mechanism).
        migrations.RunSQL(
            sql="ALTER TABLE ticks_tick SET (timescaledb.compress, timescaledb.compress_segmentby = 'account_id, token');",
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            sql="SELECT add_compression_policy('ticks_tick', INTERVAL '7 days', if_not_exists => true);",
            reverse_sql="SELECT remove_compression_policy('ticks_tick', if_exists => true);",
        ),
        migrations.RunSQL(
            sql="SELECT add_retention_policy('ticks_tick', INTERVAL '365 days', if_not_exists => true);",
            reverse_sql="SELECT remove_retention_policy('ticks_tick', if_exists => true);",
        ),
    ]
