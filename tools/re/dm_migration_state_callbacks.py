"""Trace the matched DataMigrator migration-state method through its mutex.

The caller's msgID-0 path enters DMMigrationState. Score mutex lock/return,
the selected starter block, unlock, and method return on the same server
thread. All sites are read-only and armed only after the matched msgID lookup.
"""

import dm_migration_server_migrate_callbacks as parent


parent.CALLBACK_MODULE = "dm_migration_state_callbacks"
on_hit = parent.on_hit


parent.MAIN.update({
    "DM_STATE_ENTRY": 0x8A98,
    "DM_STATE_MUTEX_CALL": 0x8AF0,
    "DM_STATE_MUTEX_RETURN": 0x8AF4,
    "DM_STATE_AFTER_FIRST_DECISION": 0x8B44,
    "DM_STATE_STARTER_BLOCK_CALL": 0x8CE8,
    "DM_STATE_STARTER_BLOCK_RETURN": 0x8CEC,
    "DM_STATE_MUTEX_UNLOCK_CALL": 0x8CD4,
    "DM_STATE_MUTEX_UNLOCK_RETURN": 0x8CD8,
    "DM_STATE_ASYNC_CALL": 0x8D74,
    "DM_STATE_ASYNC_RETURN": 0x8D78,
    "DM_STATE_RETURN": 0x8EE4,
})


def install(debugger, slide):
    parent.install(debugger, slide)
