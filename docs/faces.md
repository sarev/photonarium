# Faces

[< Back to README](../README.md)

Faces is where you clean up and organise people so you can later filter the Gallery by who is in the photo. It's designed to be fast to tidy up: name people, ignore false detections, merge duplicates, and choose a good thumbnail for each person.

Face detection runs on images only -- video frames are not scanned for faces.

## Known People list

What you can do:

- Click a person to select them.
- Click the filter icon on a person to open the Gallery filtered to images containing them.
- Drag and drop one person onto another to merge them into the destination person.

Keyboard tips (when the Known People list is focused):
- Arrow keys move between people.
- Enter opens that person in pick-preferred mode.
- Escape clears the selection.

## Unknown Faces grid

This is where you deal with faces that are not named or matched yet.

What you can do:

- Drag an unknown face onto a person in the Known People list to associate it with that person.
- Double click a face to open the source image in the full-screen viewer.
- Hover a face to reveal:
  - Grey `-` to mark the face as ignored
  - Red `x` to remove the bounding box (it is not a face)
  - Sparkle button to open Quick Match (see below)

### Quick Match

Once you have a few people established with locked faces, Quick Match becomes a powerful way to rapidly identify unknown faces. Click the sparkle button on any unknown face (or select multiple faces and click the sparkle on any of them) to see a card showing the top matching people from your library.

- The card shows up to 5 people, ranked by how closely their locked faces match the selected face(s)
- Click a person to assign all selected faces to them instantly
- Click outside the card or press **Escape** to dismiss without making changes

This is especially useful when you have a large backlog of unknown faces and many established people. Instead of scrolling through the Known People list or remembering names, Quick Match shows you the most likely candidates based on face similarity.

### Semantic search for faces

The "Search faces..." input at the top of the Unknown Faces grid uses the same semantic search as the main Search screen. You can describe what you're looking for (e.g., "smiling", "glasses", "outdoor") to filter unknown faces. Negative terms work here too: `outdoor -sunglasses` finds outdoor faces without sunglasses.

Useful workflow:
- When starting from scratch, name a few clear examples of a person, then move on to a different person. Having several people set up before refining any one person helps recognition and reduces mix-ups.

## Pick preferred face (per person)

Open a person to manage them in more detail. This mode is for improving one person at a time:

- Pick a preferred face as their avatar (star).
- Keep a face firmly associated with this person (lock).
- Remove mistakes:
  - Press **Delete** to move a face back to Unknown Faces.
  - Use grey `-` to ignore a face.
  - Use green `x` to un-name it and return it to Unknown Faces.
- Use the sparkle button to Quick Match a face to a different person if it was mis-assigned.
- Double click a face to open the source image in the full-screen viewer.

Matching threshold:
- You can adjust the "Matching threshold" slider to re-evaluate which faces belong to this person. Lowering it tends to add more matches, raising it tends to remove weaker matches.
- Locked faces are used as reliable examples when re-evaluating, and changes can add or remove faces for this person.

## Advice on tagging faces

When you first add a folder of images to Photonarium, it will try to spot all of the faces in the images (which can take some time!). This will normally result in the Faces screen showing a lot of 'unknown' faces. Try to find a face for a person you know and enter their name against their image. This will create your first 'person' for the People list. Then, name a few more examples of their face, ideally in different poses and lighting conditions. At this point, you can move onto another person. Follow these steps for a reasonable selection of the people you want to tag (a few images of each). You can drag-and-drop unknown faces onto a person (even multiple at once) to quickly name them.

As you come across faces of people that you're not bothered about naming, you can name them '-'. This is a special name that tells Photonarium that this person is someone you want to ignore. Clicking on the grey circle with a '-' in it that appears as you hover over an unknown face marks them as a face to ignore. If you have multiple unknown faces selected, they will all be ignored with one click. Moving people under the ignore name is helpful for reducing clutter in the unknown faces list.

Occasionally, you'll come across images in the unknown faces list that aren't faces. These are incorrect detections by the face detection model. You can click the red circle 'x' control that appears when you hover over it to remove this from the faces list altogether (it is essentially forgotten).

When you name (or ignore) a face, it will be 'locked' to the name you've given it. This means two things:

1. Photonarium uses this face to try to find other similar faces and match them to the same name
2. Photonarium won't automatically match this face to any other person, even if it's very similar

In the background, while you are naming faces, or marking them as ignored, Photonarium will work to see if any of the remaining unknown and unlocked faces can be moved under any of the named people. When it does this, the faces will be named appropriately but *unlocked*, so that they might be automatically moved later as it becomes clearer what each person looks like (more locked faces with that name).

Double-clicking on a person's face in the list of people will open a view where you see all of the faces that have been given that name. The ones with a green padlock symbol are locked, clicking this will unlock the face. Clicking a grey (unlocked) padlock symbol will lock the face. Hovering over a face, you'll see the grey '-' (ignore) and the green 'x' (unname) controls appear. This helps you to fine-tune the faces under this person. You can also select the star badge (turns gold) to choose a single face that Photonarium will use as the 'preferred' face for this person -- this is the one it uses elsewhere in the app when referencing that person.

A useful trick is to click the open padlock button in the toolbar to show only the unlocked faces for this person (if any). These are all the ones that were automatically assigned. By lowering the "match threshold" slider (and wait a few seconds -- this can take a little time), Photonarium will review all unknown and unlocked faces to see if any can move across under this name. A lower threshold means faces don't have to be quite as similar to be considered a match. You can then lock the faces that you are happy really *do* belong to that person, before raising the matching threshold back up to a more strict level.

Clicking the "Focus on one person" button in the toolbar returns you to the main Faces screen.

Finally, once you're reasonably happy you've built up a good cross-section of faces for each person, you can go into the faces list for the 'ignored' list (double-click the '-' entry in the people list), and look for people who you missed (should actually be named). If you find any, just unlock them and you can (hopefully) get Photonarium to automatically move them across to the right place.
