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

## Selection auto-scroll

Drag selection should auto-scroll when the user is trying to drag off the top or bottom of the visible area of the gallery.

## Selection modifier keys

We should support ctrl-click and shift-click selection models, as per how Windows does these.

## GUI consistency

The similarity sliders for the filters have loose on the left, and strict on the right. For the duplicate finder screen, the slider has exact on the left, and related on the right. Can we reverse the order of the duplicate finder slider so that it's logically consistent with the filter sliders?

## Close icon

In the full-screen view, I'd like a small close icon overlay that fades after a couple of seconds (as per the image name at the bottom) but reappears (briefly) when the user interacts with the photo (moves mouse, zooms, pans, presses a key, taps/clicks, etc.). It should be reasonably discrete, but not so much it'd be missed.

## Scroll override

When applying a filter from the filter screen, we return to the Gallery screen. If anything was changed about the filter, we should scroll to the top of the grid of images. This is probably better than leaving the gallery view at a stale scroll offset.

On a related note, clicking the "Sort by content similarity" button in the gallery toolbar should also scroll the gallery back to the top.

## Persisting info

When entering a description (string) or rating (string/emojis) for an image, I believe you have to hit 'enter' in the field to persist what you've entered. This is non-obvious. I think we should also persist on blur for each of these fields.

## Deleting description

I'm not checked, but can you confirm that if I edit an image description string to be an empty string (i.e. remove the description) then the associated description embedding also gets nulled?

## Semantic search bug

I gave an image a description (string). None of the others have. This image now appears first in all semantic searches (filter results) for reasons I cannot determine! The string is "Ste & Karen in Valmezo garden" and the search term is "Swans" or "Cat in box" yet still it's displayed first...

## Slow image ingestion

During the ingestion process, for indexing and embedding new images, this seems to run a lot slower than I was hoping. I think the indexing can and should be run across a thread pool, not just on a single thread. I can see why having the embedding running in a single thread makes sense - stick with that part. The size of the thread pool should probably be a persisted config option.
