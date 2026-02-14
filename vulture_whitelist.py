"""Vulture whitelist — items reported as unused that are actually used by frameworks
or external callers, not direct Python call sites.

Run: vulture *.py vulture_whitelist.py
"""

# --- Flask route handlers (called by Flask routing, not by Python code) ---
# All @app.route() decorated functions in app.py are entry points.
invalidate_images_cache  # called by Flask route decorator side-effect
serve_index
get_images
generate_caption
get_thumbnail
get_histogram_images
get_full_image
reveal_image
get_images_people_names
pick_folder
get_config
reveal_config
get_config_schema_endpoint
save_config_endpoint
rescan_folders
get_duplicates
sort_duplicates_semantic
prune_duplicates
create_group
rename_group
delete_group
add_images_to_group
remove_images_from_group
metadata_search
metadata_keys
metadata_values
get_cache_stats
get_events
get_event_count
get_people
create_person_endpoint
get_person_endpoint
update_person_endpoint
delete_person_endpoint
get_person_faces
get_person_thumbnail
get_image_faces
get_faces_list
get_single_face
get_face_matches_endpoint
assign_faces
unassign_faces_simple
suppress_faces_batch
update_faces_batch
identify_face
identify_faces_batch
trigger_full_reassessment
get_faces_reassess_status
ack_reassessment
unidentify_face
suppress_face_endpoint
toggle_face_manual_tag_endpoint
delete_face_endpoint
get_face_thumbnail
unassign_face
unassign_faces_batch
get_faces_group_status
set_preferred_face
merge_person
dissolve_person
not_found  # Flask error handler
internal_error  # Flask error handler

# --- Python context manager protocol (__exit__ signature) ---
exc_type
exc_val
exc_tb

# --- PyTorch / ML framework callbacks ---
forward  # nn.Module.forward() called by PyTorch's __call__

# --- SQLite / PIL attribute assignments (side-effect setters) ---
row_factory  # sqlite3.Connection.row_factory — changes query result type
LOAD_TRUNCATED_IMAGES  # PIL.ImageFile — allows loading partial images

# --- Signal handler variable (inspected by signal module) ---
frame  # signal handler second argument

# --- Thread worker queue properties (read by status polling) ---
processed_count
error_count
faces_detected_count
