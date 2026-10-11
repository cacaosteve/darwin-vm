"""Add the inner DMMigrator migration-start call to the msgID-0 ladder."""

import dm_migration_server_migrate_callbacks as parent


parent.CALLBACK_MODULE = "dm_migration_server_inner_callbacks"
on_hit = parent.on_hit


parent.MAIN.update({
    "DM_DELEGATE_ENTRY": 0xFE44,
    "DM_DELEGATE_NORMAL_ARM": 0xFEF0,
    "DM_START_MIGRATION_CALL": 0xFFC8,
    "DM_START_MIGRATION_RETURN": 0xFFCC,
    "DM_DELEGATE_RETURN": 0x10008,
})


def install(debugger, slide):
    parent.install(debugger, slide)
