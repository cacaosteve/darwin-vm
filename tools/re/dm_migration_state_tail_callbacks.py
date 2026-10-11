"""Focused post-dispatch_async ladder in DMMigrationState's msgID-0 path."""

import dm_migration_server_migrate_callbacks as parent


parent.CALLBACK_MODULE = "dm_migration_state_tail_callbacks"
on_hit = parent.on_hit


parent.MAIN.clear()
parent.MAIN.update({
    "DM_MSGID0_ARM": 0x1C78,
    "DM_MIGRATION_CALL": 0x2A8C,
    "DM_STATE_ENTRY": 0x8A98,
    "DM_STATE_MUTEX_CALL": 0x8AF0,
    "DM_STATE_MUTEX_RETURN": 0x8AF4,
    "DM_STATE_ASYNC_CALL": 0x8D74,
    "DM_STATE_ASYNC_RETURN": 0x8D78,
    "DM_STATE_POST_ASYNC_UNLOCK_CALL": 0x8D8C,
    "DM_STATE_POST_ASYNC_UNLOCK_RETURN": 0x8D90,
    "DM_STATE_POST_UNLOCK_CHECK_CALL": 0x8D94,
    "DM_STATE_POST_UNLOCK_CHECK_RETURN": 0x8D98,
    "DM_STATE_SEMAPHORE_ARM": 0x8D9C,
    "DM_STATE_LATE_ARM": 0x8E20,
    "DM_STATE_LATE_CALL": 0x8E74,
    "DM_STATE_LATE_RETURN": 0x8E78,
    "DM_STATE_RETURN": 0x8EE4,
})


def install(debugger, slide):
    parent.install(debugger, slide)
