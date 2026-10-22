"""Allowlisted public failure codes for delivery preparation; no audit text."""

# Keep this explicit: exception codes from providers must not become public data.
PUBLIC_AUTHORING_ERROR_CODES = frozenset(
    f"{namespace}.{name}"
    for namespace, names in {
        "delivery_amend": ("authoring_failed obligation_assignments_invalid recovery_failed recovery_scope_changed "),
        "delivery_amend_authoring": (
            "amend_reason_invalid budget_exceeded_invalid disposition_invalid plan_id_mismatch "
            "replacements_unexpected summary_required summary_unexpected target_id_reused "
            "target_unit_mismatch "
        ),
        "delivery_assessment": (
            "authoring_failed dependency_cycle dependency_duplicate dependency_self dependency_unknown "
            "label_invalid mode_invalid reason_code_duplicate reason_code_invalid reason_code_mode_mismatch "
            "reason_codes_required reason_codes_too_many unit_id_duplicate unit_not_object units_forbidden "
            "units_invalid_type units_required units_too_many "
        ),
        "delivery_authoring": (
            "asset_path_duplicate asset_path_invalid asset_paths_invalid_type authority_too_large "
            "budget_invalid_type budget_unknown_field budget_value_invalid checkpoints_invalid "
            "constraint_disposition_invalid constraint_id_duplicate constraint_id_invalid "
            "constraint_kind_invalid constraint_not_object constraint_summary_source_excerpt "
            "constraint_unit_duplicate constraint_unit_unknown constraint_units_empty "
            "constraint_units_too_many constraints_invalid_type constraints_required constraints_too_many "
            "dependency_cycle dependency_duplicate dependency_self dependency_unknown empty_output "
            "estimated_size_invalid expected_plan_id_invalid expected_plan_id_mismatch json_invalid "
            "label_invalid obligation_disposition_invalid obligation_id_duplicate obligation_id_invalid "
            "obligation_not_object obligation_source_ref_unknown obligation_source_refs_invalid "
            "obligation_summary_source_excerpt obligation_unit_unknown obligation_units_invalid "
            "obligations_required obligations_too_many output_dir_outside_project output_invalid_envelope "
            "path_invalid path_outside_project plan_file_outside_project plan_id_invalid plan_id_mismatch "
            "planner_step_budget_invalid planning_mode_invalid project_root_invalid risk_tag_duplicate "
            "risk_tag_invalid risk_tag_unknown risk_tags_invalid_type root_not_object scope_path_absolute "
            "scope_path_invalid scope_path_outside_project scope_paths_invalid_type "
            "stop_and_follow_up_summary_invalid string_list_item_invalid string_list_required "
            "string_required unit_id_duplicate unit_id_invalid unit_markdown_asset_manifest "
            "unit_markdown_generated_marker unit_markdown_missing_section "
            "unit_markdown_missing_verification_commands unit_not_object unit_path_field_forbidden "
            "unit_task_path_outside_project units_dir_outside_project units_empty units_invalid_type "
            "unknown_field "
        ),
        "delivery_authority": ("context_invalid gaps_invalid "),
        "delivery_constraint_repair": (
            "assignment_invalid authoring_failed constraint_count_invalid existing_constraint_changed "
            "omitted_constraint_mismatch "
        ),
        "delivery_constraint_verification": (
            "authoring_failed complete_invalid constraints_mismatch gap_assignment_not_missing "
            "gap_constraint_mismatch gap_constraint_required gap_constraint_unexpected "
            "gap_constraint_unknown gap_duplicate gap_kind_invalid gap_not_object gap_reason_invalid "
            "gap_summary_source_excerpt gap_unit_duplicate gap_unit_unknown gap_units_empty "
            "gap_units_too_many gaps_invalid_type gaps_required gaps_too_many gaps_unexpected "
        ),
        "delivery_obligation_repair": (
            "assignment_invalid authoring_failed count_invalid existing_changed omitted_mismatch "
        ),
        "delivery_obligation_verification": (
            "complete_invalid gap_assignment_existing gap_duplicate gap_id_invalid gap_id_unexpected "
            "gap_mismatch gap_not_object gap_reason_invalid gap_source_refs_invalid "
            "gap_summary_source_excerpt gap_units_invalid gaps_invalid gaps_required gaps_unexpected "
            "obligations_mismatch "
        ),
        "delivery_prepare": ("authoring_failed output_invalid recovery_failed recovery_scope_changed "),
        "delivery_unit_context_verification": (
            "authority_required complete_invalid gap_not_object gaps_invalid_type gaps_required "
            "gaps_too_many gaps_unexpected literal_already_present literal_duplicate literal_invalid "
            "literal_not_source_line literals_required literals_too_large literals_too_many unit_duplicate "
            "unit_unknown "
        ),
        "source_accounting": (
            "checkpoint_incomplete checkpoint_invalid constraints_incomplete disposition_invalid fragment_invalid incomplete "
            "invalid mapping_invalid rationale_invalid reference_invalid unresolved verification_mismatch "
        ),
    }.items()
    for name in names.split()
)
