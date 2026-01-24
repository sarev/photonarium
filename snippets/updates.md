# Feature requests, improvements, bugs and issues:

## Thumbnail loading - DONE

It looks like the gallery loads the image thumbnails dynamically and (I believe) asynchronously, which is a good thing. The issue I've found is when scrolling quickly through hundreds of images, the process appears to be loading them in order viewed, which means it can take _ages_ to get around to loading the thumbnails for the images I'm actually looking at when I finish scrolling. I think the loading should be more LIFO than FIFO, favouring loading the most recent thumbnails first.

## Thumbnail RAM cache size - IGNORED (SKIPPED)

I'm not sure if it does, but we might need to impose a max RAM budget for loaded thumbnails. A few 100MiB might be about right. Have it as one of the things in our config file...

## Timestamp editing - DONE

In the "Image information" pane of the gallery screen, I'd like to have the timestamp for the selected photo editable. This should include a date picker. If the user updates this, it replaces whatever the timestamp is in the database for that image.

## Timestamp calculations - DONE

I've realised I made a significant error in the image timestamp derivation rules. We should do the filename stuff _before_ the filesystem create/modified stuff (but still after the EXIF stuff), because often files are moved and copied around with file system datestamps being messed up, but if the photo is in a folder called "2014" then we should really assume it's from 2014 sometime. On a related note, where we cannot determine the complete date (e.g. we have the year but we're missing the month and day, or just the day), we should assume "01 January" for the missing parts.

## Fix existing database - DONE

After fixing the timestamp calculation above, we'll probably need to do a (one-time) sweep through all of the images currently in the database to fix their timestamps!

## Multiple image selection bug - DONE

When multiple images are selected, the "Image information" shouldn't be showing information about the selected image (it currently seems to display info about one of them) - it should just stick with the info about how many are selected.

## Gallery background updates - DONE

Also, it'd be nice if the gallery screen updated from time to time while the database is in an "Updating" state. So long as it doesn't mess up the scroll position and selection, it would be good to have the newly added images appear in the gallery.

## Selection auto-scroll - DONE

Drag selection should auto-scroll when the user is trying to drag off the top or bottom of the visible area of the gallery.

## Selection modifier keys - DONE

We should support ctrl-click and shift-click selection models, as per how Windows does these.

## GUI consistency - DONE

The similarity sliders for the filters have loose on the left, and strict on the right. For the duplicate finder screen, the slider has exact on the left, and related on the right. Can we reverse the order of the duplicate finder slider so that it's logically consistent with the filter sliders?

## Close icon - DONE

In the full-screen view, I'd like a small close icon overlay that fades after a couple of seconds (as per the image name at the bottom) but reappears (briefly) when the user interacts with the photo (moves mouse, zooms, pans, presses a key, taps/clicks, etc.). It should be reasonably discrete, but not so much it'd be missed.

## Scroll override - DONE

When applying a filter from the filter screen, we return to the Gallery screen. If anything was changed about the filter, we should scroll to the top of the grid of images. This is probably better than leaving the gallery view at a stale scroll offset.

On a related note, clicking the "Sort by content similarity" button in the gallery toolbar should also scroll the gallery back to the top.

## Persisting info - CANNOT REPRODUCE

When entering a description (string) or rating (string/emojis) for an image, I believe you have to hit 'enter' in the field to persist what you've entered. This is non-obvious. I think we should also persist on blur for each of these fields.

NOTE TO SELF: try to make this into a reproducible test case before re-raising for further investigation.

## Deleting description - CONFIRMED

I'm not checked, but can you confirm that if I edit an image description string to be an empty string (i.e. remove the description) then the associated description embedding also gets nulled?

## Semantic search bug - DONE

I gave an image a description (string). None of the others have. This image now appears first in all semantic searches (filter results) for reasons I cannot determine! The string is "Ste & Karen in Valmezo garden" and the search term is "Swans" or "Cat in box" yet still it's displayed first...

## Serious performance problem - DONE

I now have ~ 30,000 images in the database (maybe half of my collection) but starting the gallery just hangs the page for around 20 seconds - long enough for the browser to say the page has stopped responding. I suspect it's the core JS asking the backend for a list of image info.

We need to have this mechanism work in a more scalable way, so a database with 100,000 images doesn't stop the UI from performing reasonably smoothly. I don't mind the odd modal progress indicator, which could *also* be helpful, but we need to get to the bottom of the core issue (which I believe is due to front-end/backend roundtrips taking ages when the database is of non-trivial size).

## Slow image ingestion - DONE

During the ingestion process, for indexing and embedding new images, this seems to run a lot slower than I was hoping. I think the indexing can and should be run across a thread pool, not just on a single thread. I can see why having the embedding running in a single thread makes sense - stick with that part. The size of the thread pool should probably be a persisted config option.
2
## Indexing ETA - DONE

When the database is updating, it would be helpful for the "Indexing" progress info on the Database screen to include a computed "ETA", based upon how many images are being indexed per second over the last n seconds (or similar). We don't need this for the "Embedding" part, because that number is generally very low or zero, so any ETA would flicker and change pretty much randomly.

## Database screen updates - DONE

When the database is in an "Updating" state, we have the indexing and embedding status updating periodically. It would be good to have the "Total images" field updating at the same time.

## Toolbar 'sreen control' button semantics - DONE

When we're on the Gallery screen, the related toolbar button (for going to the Gallery screen) is hidden. This makes sense. When we're on the other main screens (Database, Duplicates, Search/Filter) their corresponding buttons *aren't* hidden. They should be.

## "Rescan all" validation - DONE

I'd like to double-check what happens when the user clicks on "Rescan all folders" in the Database screen. Please outline everything it will do as plain English bullet points.

## Rotate buttons - DONE

In the gallery toolbar, we have a button for going into full-screen mode. This is shaded if there isn't exactly one imgae selected. It's good. I'd like two buttons next to it: for rotating all selected images anti-clockwise, or clockwise. It will be shaded if no images are selected.

Clearly, rotating the image will mean its sha256 checksum (and probably size and modified date) will change. As an optimisation, to help speed things up, I think we can assume that the rotation operation *doesn't* change the embedding, description, rating, description embedding, laplacian variance, or perceptual hash

## Fullscreen prev/next buttons - DONE

Along with the temporary close button on the fullscreen view, I'd like "Previous" and "Next" arrow buttons at each side of the screen. These do the same thing as pressing the corresponding cursor key when clicked/tapped.
