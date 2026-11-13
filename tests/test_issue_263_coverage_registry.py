"""Review map: each registered tool has an inventory entry and behavioral tests.

This registry checks references stay live; referenced tests establish behavior.
It deliberately does not claim every tool has a body refusal: search/list empty
answers are successes, and get_vault_guide has no returned body-refusal branch.
"""
import ast
from pathlib import Path

# Tool -> [(terminal contract class, module::behavioral test), ...]
COVERAGE = {
    'keyword_search': [('success', 'test_issue_161_result_telemetry.py::test_keyword_search_logs_count_and_paths')],
    'read_note': [('refused', 'test_issue_263_tool_outcomes.py::test_read_error_private_metadata_budget_and_schema'), ('success', 'test_issue_263_tool_outcomes.py::test_successful_structured_note_with_forged_sentinel_stays_success')],
    'list_notes': [('success', 'test_issue_263_tool_outcomes.py::test_metadata_empty_successes_and_graph_body_refusals')],
    'get_tags': [('success', 'test_issue_263_tool_outcomes.py::test_metadata_empty_successes_and_graph_body_refusals')],
    'get_recent': [('success', 'test_issue_263_tool_outcomes.py::test_metadata_empty_successes_and_graph_body_refusals')],
    'semantic_search': [('success', 'test_issue_200_tool_render.py::test_an_all_stale_result_set_survives_and_says_so')],
    'get_vault_guide': [('success', 'test_file_access_tools.py::test_guide_mentions_file_tools')],
    'create_note': [('refused', 'test_issue_263_tool_outcomes.py::test_actual_creation_refusal_keeps_bytes_and_records_one_outcome')],
    'get_backlinks': [('refused', 'test_issue_263_tool_outcomes.py::test_metadata_empty_successes_and_graph_body_refusals')],
    'get_links': [('refused', 'test_issue_263_tool_outcomes.py::test_metadata_empty_successes_and_graph_body_refusals')],
    'get_neighborhood': [('refused', 'test_issue_263_tool_outcomes.py::test_metadata_empty_successes_and_graph_body_refusals')],
    'find_related': [('refused', 'test_issue_200_tool_render.py::test_the_not_found_branch_is_unchanged'), ('success', 'test_issue_200_tool_render.py::test_a_fresh_source_with_no_neighbours_stays_a_bare_zero_result')],
    'find_orphans': [('success', 'test_issue_263_tool_outcomes.py::test_metadata_empty_successes_and_graph_body_refusals')],
    'edit_note': [('refused', 'test_issue_263_tool_outcomes.py::test_edit_operation_refusal_precedes_missing_file')],
    'move_note': [('partial', 'test_issue_263_tool_outcomes.py::test_move_metadata_failure_is_partial_in_the_actual_usage_row'), ('partial', 'test_issue_263_tool_outcomes.py::test_move_skipped_unreadable_source_is_partial_in_the_actual_usage_row'), ('partial', 'test_issue_263_tool_outcomes.py::test_compound_move_conflict_keeps_caller_code_and_later_publication_marker'), ('partial', 'test_anchored_note_writes.py::test_a_failed_rollback_names_the_recovery_location'), ('refused', 'test_anchored_note_writes.py::test_a_source_replaced_by_a_non_file_is_moved_back'), ('partial', 'test_anchored_note_writes.py::test_an_unverifiable_destination_is_reported_not_raised')],
    'delete_note': [('refused', 'test_issue_88_root_confirmed_before_publish.py::test_the_permanent_delete_is_refused_and_the_note_stays')],
    'set_frontmatter': [('refused', 'test_issue_263_tool_outcomes.py::test_frontmatter_helper_propagates_one_generated_sentinel')],
    'read_file': [('refused', 'test_issue_263_tool_outcomes.py::test_actual_body_branches_are_typed'), ('success', 'test_read_response_cap.py::test_read_file_text_is_capped_and_pages')],
    'write_file': [('success', 'test_anchored_note_writes.py::test_renaming_the_parent_mid_raw_write_cannot_redirect_it')],
    'list_files': [('success', 'test_asvs_tool_caps.py::test_a_pattern_at_the_limit_lists_normally')],
    'request_upload': [('success', 'test_transfer_tools.py::test_request_upload_mints_a_bound_link'), ('refused', 'test_transfer_tools.py::test_request_upload_is_refused_for_a_read_only_key')],
    'check_upload': [('success', 'test_transfer_tools.py::test_check_upload_reports_pending'), ('refused', 'test_issue_263_tool_outcomes.py::test_actual_body_branches_are_typed')],
    'request_download': [('success', 'test_transfer_tools.py::test_request_download_mints_a_fingerprinted_link')],
    'import_from_url': [('partial', 'test_transfer_tools.py::test_import_reports_a_post_publish_failure_as_written')],
    'fetch_page': [('success', 'test_fetch_page.py::test_returns_the_page_as_structured_untrusted_fields'), ('refused', 'test_fetch_page.py::test_a_non_text_response_is_refused_before_its_body_is_read')],
    'delete_file': [('refused', 'test_transfer_tools.py::test_delete_file_reports_a_missing_file')],
    'move_file': [('success', 'test_move_file.py::test_moves_a_file_and_creates_parents'), ('refused', 'test_move_file.py::test_an_existing_destination_is_never_replaced')],
    'delete_folder': [('success', 'test_delete_folder.py::test_removes_an_empty_folder'), ('refused', 'test_delete_folder.py::test_a_folder_with_content_is_never_removed')],
    'list_tasks': [('success', 'test_fork_task_tools.py::test_list_tasks_success_shape'), ('refused', 'test_fork_task_tools.py::test_list_tasks_refuses_an_unsupported_global_query')],
    'create_task': [('success', 'test_fork_task_tools.py::test_create_appends_the_dialog_line_in_canonical_order'), ('refused', 'test_fork_task_tools.py::test_create_refuses_a_missing_note_other_than_the_inbox')],
    'edit_task': [('success', 'test_fork_task_tools.py::test_postpone_sets_scheduled'), ('refused', 'test_fork_task_tools.py::test_edit_recurring_is_refused')],
    'set_task_status': [('success', 'test_fork_task_tools.py::test_done_adds_done_date_and_changes_only_that_line'), ('refused', 'test_fork_task_tools.py::test_recurring_task_is_refused')],
    'ledger_search': [('refused', 'test_ledger_tools.py::test_search_refuses_a_bad_limit_or_mode'), ('success', 'integration/test_ledger_pg.py::test_search_words_and_meaning_are_two_lists')],
    'ledger_get': [('refused', 'test_ledger_tools.py::test_get_refuses_no_refs_or_a_malformed_one'), ('success', 'integration/test_ledger_pg.py::test_update_needs_the_revision_keeps_history_and_closes')],
    'ledger_add': [('refused', 'test_ledger_tools.py::test_writes_refuse_a_read_only_credential'), ('success', 'integration/test_ledger_pg.py::test_add_numbers_refs_refuses_unknowns_and_returns_similar')],
    'ledger_update': [('refused', 'integration/test_ledger_pg.py::test_update_needs_the_revision_keeps_history_and_closes'), ('success', 'integration/test_ledger_pg.py::test_seen_is_bulk_without_reason_and_feedback_only')],
    'ledger_digest': [('success', 'integration/test_ledger_pg.py::test_views_cite_entries_and_are_flagged_when_one_changes')],
    'ledger_digest_edit': [('refused', 'integration/test_ledger_pg.py::test_views_cite_entries_and_are_flagged_when_one_changes'), ('success', 'integration/test_ledger_pg.py::test_views_cite_entries_and_are_flagged_when_one_changes')],
    'ledger_topics': [('success', 'integration/test_ledger_pg.py::test_topics_create_merge_and_count')],
    'open_area': [('refused', 'integration/test_ledger_pg.py::test_open_area_refuses_both_or_neither_and_an_unknown_area'), ('success', 'integration/test_ledger_pg.py::test_open_area_gathers_the_note_folders_entries_and_tasks')],
    'index_status': [('success', 'integration/test_index_status_pg.py::test_counts_every_state_and_the_rate'), ('success', 'test_index_status.py::test_summary_and_notes_follow_the_state')],
    'ledger_topic_edit': [('refused', 'test_ledger_tools.py::test_writes_refuse_a_read_only_credential'), ('success', 'integration/test_ledger_pg.py::test_topics_create_merge_and_count')],
    'upstream_list_tasks': [('refused', 'test_list_tasks_render.py::test_invalid_arguments_are_refused_before_any_statement'), ('success', 'test_list_tasks_render.py::test_the_query_runs_in_one_read_only_snapshot')],
    'get_changes': [('refused', 'test_change_feed_offline.py::test_get_changes_refuses_bad_arguments_before_any_query'), ('success', 'test_change_feed_offline.py::test_render_changes_page_shape')],
    'hybrid_search': [('refused', 'test_hybrid_search_offline.py::test_the_query_cap_is_pre_body'), ('success', 'test_hybrid_search_offline.py::test_provider_down_returns_the_keyword_arm_marked')],
    'task_agenda': [('refused', 'test_task_tools_e2e.py::test_task_agenda_refuses_a_small_response_cap_before_any_statement'), ('success', 'test_task_tools_e2e.py::test_task_agenda_renders_through_the_query')],
    'complete_task': [('refused', 'test_task_tools_e2e.py::test_a_suffixed_id_without_a_hash_is_refused_before_any_read'), ('success', 'test_task_tools_e2e.py::test_complete_task_writes_refreshes_then_records_the_event')],
    'update_task': [('refused', 'test_task_tools_e2e.py::test_update_task_status_done_names_complete_task'), ('success', 'test_task_tools_e2e.py::test_update_task_sets_a_relative_due_date_and_echoes_it')],
    'add_task': [('refused', 'test_task_tools_e2e.py::test_add_task_refuses_a_missing_note_and_creates_nothing'), ('success', 'test_task_tools_e2e.py::test_add_task_inserts_after_the_task_list_and_lists_similar_tasks')],
}

# Tools registered after the typed-tool-outcomes inventory was archived. Their
# outcome contract lives in their own change's spec rather than in that
# inventory, which is a historical record and is not edited.
ADDED_AFTER_INVENTORY = {'list_tasks', 'get_changes', 'hybrid_search',  # #350, #351, #354
                         'task_agenda', 'complete_task', 'update_task', 'add_task',  # #428
                         'upstream_list_tasks'}  # upstream's list_tasks, renamed in the fork
#: Tools whose body is not `<tool>_impl` (fork: two `list_tasks`).
IMPLEMENTATION = {'keyword_search': 'search_notes_impl', 'list_tasks': 'fork_list_tasks_impl',
                  'upstream_list_tasks': 'list_tasks_impl'}


def test_every_registered_tool_has_inventory_and_live_behavioral_references():
    from src.mcp_server.server import mcp
    from src import plugins
    registered = {tool.name for tool in mcp._tool_manager.list_tools()}
    # The suite runs with PLUGINS empty: this vault's plugin tools are counted from the plugin.
    plugin_tools = {tool.name for tool in plugins.load(("kelno_vault",))[0].tools}
    assert not registered & plugin_tools
    registered |= plugin_tools
    # Upstream's tools the fork switched off are not registered, but are covered.
    from src.mcp_server.server import SWITCHED_OFF
    registered |= SWITCHED_OFF
    assert set(COVERAGE) == registered
    # 41 core tools, `open_area` (kelno_vault) and upstream's seven switched off.
    assert len(registered) == 49
    root = Path(__file__).resolve().parents[1]
    inventory = (root / 'openspec/changes/archive/2026-09-06-typed-tool-outcomes/inventory.md').read_text()
    for tool, references in COVERAGE.items():
        implementation = IMPLEMENTATION.get(tool, tool + '_impl')
        if tool in ADDED_AFTER_INVENTORY:
            assert ('async def ' + implementation + '(') in (root / 'src/mcp_server/tools.py').read_text()
        else:
            assert '### ' + implementation + ' ' in inventory
        assert references
        for contract, reference in references:
            assert contract in {'refused', 'partial', 'success'}
            module, function = reference.split('::')
            tree = ast.parse((root / 'tests' / module).read_text())
            assert any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function for node in tree.body), reference
