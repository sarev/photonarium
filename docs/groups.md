# Groups

[< Back to README](../README.md)

Groups helps you clean up your library by finding duplicates and also lets you organise images into custom albums.

## Duplicate detection (levels 0-3)

- Review duplicate "stacks" (groups) of related images.
- Adjust how strict duplicate matching should be:
  - **Related**, **Similar**, **Near-identical**, **Identical**
- Double click a stack (or press **Enter**) to open that group in the Gallery, automatically sorted by quality with the best image selected.
- While viewing a group in the Gallery, use **Alt + Left / Right** to move between groups.
- **Refine Groups**: click the refine button to filter groups by quality. Choose how many images to keep (or trash) per group -- best only, top N, or top N%. Two actions are available:
  - **View in Gallery** shows the selected subset (the best or worst images) as a filtered Gallery view, without changing anything. Works for all group levels including directories and custom groups.
  - **Trash** moves the non-kept images to the trash directory (levels 0-4 only). Uses the same quality scoring as the Gallery Quality sort.

Note: duplicate detection currently applies to images only. Videos are not included in duplicate analysis.

## Directory groups

Slide to **Directories** to see your images and videos organised by folder. Directory groups are automatically created and kept in sync whenever a folder is scanned:

- Each directory that contains media files becomes a group, named after the folder.
- When two folders share the same name, parent directories are added to make names unique (e.g. `Holiday/Beach` vs `Birthday/Beach`).
- Hover over a directory group name to see the full path.
- Directory groups are read-only -- they mirror the filesystem and update automatically.

## Custom groups (albums)

Slide to **Custom** (the leftmost position) to manage your own named groups:

- Create, rename, and delete groups from the toolbar.
- Add images to groups via the group button that appears when you hover over a Gallery thumbnail.
- The Group Picker dialog lets you manage which groups an image belongs to.
- An image can belong to multiple groups (overlap allowed).
- Groups are kept even when all images are removed.
- While viewing a custom group in the Gallery, press **Backspace** to remove selected images from the group (does not delete them).

## Semantic sorting

You can sort stacks by similarity to a concept using the semantic sort button. This helps surface particular types of duplicates:

- `blurry` surfaces out-of-focus shots
- `dark underexposed` finds poorly lit images
- `cropped tight` finds heavily cropped versions

Negative terms work here too. Use `blurry -sharp` or `dark -bright -colorful` to push good images down and bad ones up, making it easier to pick which duplicates to delete.

## Quality sorting

When you open a group in the Gallery, images are automatically sorted by **Quality** with the best image pre-selected. The quality score is a blend of several factors:

- **Aesthetic appeal** (60%) -- how visually pleasing the image is, scored by two neural networks (NIMA and LAION) that were trained on large datasets of human aesthetic judgements.
- **Sharpness** (20%) -- how well-focused the image is, measured by Laplacian variance.
- **Resolution** (15%) -- total pixel count (higher resolution = better).
- **Compression quality** (5%) -- bits per pixel, which favours less-compressed originals over heavily compressed copies.

The aesthetic component uses the raw model scores (divided by 10 to normalise to 0-1), so a score of 60% reflects a genuine 6/10 from the models rather than a relative ranking. Sharpness, resolution, and compression quality are percentile-ranked within the current image set, since they have no natural absolute scale.

These weights can be adjusted in `photonarium.yml` to suit your preferences:

- `quality_weight_aesthetic`, `quality_weight_sharpness`, `quality_weight_pixels`, `quality_weight_bpp` -- the four component weights (should sum to 1.0).
- `quality_alpha` -- controls how the two aesthetic models are blended (0.0 = LAION only, 1.0 = NIMA only, default 0.60 = a mix of both, leaning more to NIMA).
- `nima_enabled` -- set to `false` to skip NIMA scoring entirely (quality falls back to LAION with sharpness and resolution).

NIMA and LAION approach aesthetics differently. NIMA was trained on hundreds of thousands of photos rated by people, so it has a good sense of what makes a photograph look appealing -- composition, lighting, colour. LAION is faster and lighter but more impressionistic; it can favour vibrant or striking images even if they're technically flawed. Blending the two gives more balanced results than either alone, which is why both are used by default.

---

# Smart Groups

Regular custom groups are like photo albums -- you add specific images by hand. Smart Groups are more like saved searches: you define what you are looking for (a text description, a date range, certain people, camera settings, a rating -- any combination of the filters on the Search screen) and Photonarium finds matching images every time you open the group. If new photos are added to your library later, they appear automatically when they match the criteria.

## Creating a Smart Group

1. Go to the **Search** screen and set up the filters you want (text, date range, rating, people, metadata -- any combination).
2. Click **Save as Smart Group**.
3. Enter a name when prompted.

The new group appears on the Groups screen alongside your regular custom groups.

## Viewing a Smart Group

On the Groups screen, slide to **Custom** to see your Smart Groups mixed in with regular groups. Smart Groups are easy to spot: they show "Smart Group" in italics beneath the name instead of an image count.

Double-click (or press **Enter**) to open the group. Photonarium evaluates the saved filters and opens the Gallery with the matching images. This can take a moment if the filter includes a text search, since it runs a fresh semantic search each time.

## Editing a Smart Group

Hover over a Smart Group stack to reveal an **edit** badge (green circle, top-right). Click it to go back to the Search screen with the saved filters pre-loaded. Adjust anything you like, then click **Update Smart Group** to save the changes. Close the Search screen and the button returns to "Save as Smart Group" for new groups.

## How they differ from regular groups

| | Regular group | Smart Group |
|---|---|---|
| Membership | You add and remove images by hand | Determined automatically by filter criteria |
| Stays current | Only contains what you put in | Picks up new matching photos automatically |
| Add images via Gallery | Yes (group picker) | No -- membership is dynamic |
| Slideshow from stack | Yes (hover badges) | Open the group first, then start a slideshow from full-screen view |

You can rename and delete Smart Groups the same way as regular groups. The Gallery's "Add to Group" picker only shows regular groups, since adding a specific image to a dynamic group would not make sense.
