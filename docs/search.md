# Search

[< Back to README](../README.md)

Search lets you narrow a large library down to "just the photos and videos I mean". It builds a filter, then the Gallery (or Videos screen) shows only the matching results.

You can combine multiple filters at once, for example:
- "summer holiday" + "***" + People ("Sam")
- "Red steam train on sunny day"

## Search mode

At the top of the Search screen, a toggle lets you choose what to search:

- **All** (default) - searches images and videos together. Results appear in the Gallery, with videos ranked alongside images based on their preferred scene's similarity to your query.
- **Images** - searches images only. Videos are excluded from the results.
- **Videos** - searches video content at the scene level. Results appear on the [Videos screen](videos.md) with a heatmap showing which scenes in each video match your query and how strongly.

Video search analyses every scene in every video independently, so it can find a specific moment even in a long clip. Image search matches against the whole image (and its description, if one has been written).

## Text search (description)

This is semantic, so it matches meaning, not filenames.

- Type a phrase into the description search box.
- Adjust the similarity slider:
  - Lower similarity finds broader matches.
  - Higher similarity is more strict.
- Press **Enter** in the description box to apply.

### Negative terms

You can exclude concepts from your search by prefixing words with `-`:

- `beach -people` finds beaches without people
- `-indoor sunset` finds sunsets that aren't indoors
- `red train -steam-engine` finds red trains but not steam engines

Hyphens within words are preserved: `double-blind` searches for the phrase "double-blind", not "double" minus "blind".

**Tip:** More terms give better results. A simple query like `beach -people` may still return beach photos with people in them. For stronger exclusion, be more specific: `beach sand sea waves blue skies sunshine -people -man -woman -child -person -crowd` does a much better job of finding empty beaches. The same applies to positive terms: adding synonyms and related words helps the model understand exactly what you want.

## People

Only available when face detection is enabled and you have named people.

If you mention a known person's name in the description field (e.g. "bob at the beach"), Photonarium automatically recognises it and adds a people filter chip as you type. Multi-word names like "Mary Jane" are matched in preference to shorter overlapping names. When you apply the filter, recognised names are stripped from the search text so the AI focuses on the descriptive content.

You can also add people manually using the picker:

### People picker dialog

- Type part of a person's name to narrow the list.
- Click a person to add them to the filter.
- Click them again (in the selected list) to remove them.
- You can also drag and drop people between the available and selected lists.
- **Enter** confirms (unless you're typing in the search box).
- **Escape** cancels.

## Date range

- Set a start date and/or end date.
- Leave either blank to make it open-ended.

## Rating

- Type directly into the rating field.
- Or click the emoji button to insert an emoji quickly.

## Metadata

Filter by EXIF camera settings such as Camera, Lens, ISO, Aperture, Shutter Speed, and others. This is useful for finding all images taken with a particular camera body, lens, or shooting settings.

- Click the metadata area or the camera button to open the metadata picker.
- Type into any field to search - matching is fuzzy (subsequence), so "nkn" will find "Nikon" and "d85" will find "D850".
- As you type, a dropdown shows matching values from your library.
- Click **Done** to confirm your choices. Each filled field appears as a chip in the filter bar.
- Click the **x** on a chip to remove that criterion.
- Multiple metadata filters are combined with AND (all must match).

You can also add metadata filters directly from the Gallery: open the info panel, click **View EXIF data**, then click the filter icon next to any value you want to filter by.

## Applying, saving, or clearing

- **Apply** uses your current filters and returns to the Gallery (or Videos screen, if in Videos mode).
- **Save as Smart Group** saves your current filters as a [Smart Group](groups.md#smart-groups) - a dynamic group that re-evaluates the criteria each time you open it.
- **Clear** removes all filters.

Tip: You can also leave Search with **Escape**, returning to the Gallery.
