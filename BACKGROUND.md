# DISCLAIMER

This document is opinionated, but hopefully fair!

# Background

My motivations for creating Photonarium were two-fold:

1. Build my dream photo organiser.
2. See if I could build it using an LLM, rather than me writing most of the code.

The more practical motivation was born of looking at the pre-existing solutions and coming away feeling dissatisfied. Your options broadly divide into three camps:

1. Commercial
2. Free
3. NAS-bundled

I was looking for some key features:

a) Be able to search through my images semantically

b) Be able to find specific people (ideally with automated face tagging)

c) Be able to easily find duplicates and near duplicates, groups of related images

d) Not have my privacy invaded - my data doesn't leave my machine

e) Not have to shell out loads of money!

## Commercial Trade-offs
 
Commercial tools can be genuinely excellent, but the trade often starts with cost. I'm happy to pay for good software, but subscription models turn "view your own photos" into an ongoing drain, with price rises and bundle reshuffles you can't realistically opt out of. Even if it stays affordable, it still changes the relationship: you're renting access to your own data on terms the vendor can rewrite.

Once you put your photo library behind an account, privacy stops being something you own and becomes something you are promised. Maybe the vendor is acting in good faith, but you are still betting against incentives: data wants to be measured, centralised, analysed, and retained. Policies have a habit of evolving in whichever direction makes the numbers go up.

Closed-source tools are, by definition, a black box. You can file tickets and feature requests, but you cannot fix the thing that irritates you, or add the feature you want. You cannot see why it was rejected, and you cannot keep the tool aligned with your workflow when priorities shift. You are not collaborating, you're chatting with a support bot!

## The Free Landscape

There are a number of worthy, free apps out there for managing your images. Here's a (hopefully!) honest assessment of how those projects compare to Photonarium, as of late-2025/early-2026:

### [digiKam](https://www.digikam.org/)

The heavyweight champion of open-source photo management. It's a KDE project licensed under GPL-2.0, runs on Windows, Mac, and Linux, works fully offline, and collects zero telemetry. It has face detection and recognition using deep learning, duplicate detection via perceptual hashing, extensive RAW support (1260+ camera formats), non-destructive editing, and a staggering breadth of metadata tools. It's the most feature-complete free desktop photo manager by a wide margin.

The downsides? The UI is dense and technical - very much a power-user tool with a KDE aesthetic that can feel overwhelming. It's a large download due to bundled KDE frameworks. And critically, it has no CLIP-style semantic search yet (the team is exploring it, but as of late 2025 it's still partial/planned). No web UI, no mobile interface. Face training requires a fair bit of manual effort up front.

### [Immich](https://immich.app/)

The darling of the self-hosted community and the closest thing to a free Google Photos replacement. Licensed under AGPL-3.0, it's a server application with a polished web UI and native iOS/Android apps with automatic photo backup. It uses CLIP for semantic search, InsightFace for face recognition, and has ML-based duplicate detection. Multi-user support, shared albums, map views - it's impressively complete for a project that only reached stable v2.0 in October 2025.

The catch is that it's a *server*, not a desktop app. You need Docker, at least 6-8GB of RAM for the ML processing, and ideally a Linux host (Windows/macOS via Docker Desktop is supported but discouraged). If you're comfortable running infrastructure, it's excellent. If you just want to point something at your photo folders and go, it's overkill.

### [PhotoPrism](https://www.photoprism.app/)

A more established self-hosted alternative to Immich, also AGPL-3.0. It has AI-powered classification (recently upgraded to TensorFlow 2), face recognition, a decent web PWA, and optional integration with Ollama/OpenAI for captioning. Runs via Docker, self-hosted, no telemetry.

However, some features are paywalled behind expensive paid memberships (Essentials ~EUR200/year, Plus ~EUR600/year) - including multi-user management, which feels restrictive for an AGPL project. Its duplicate detection is basic (exact checksums only, no visual similarity browsing). No native mobile app, no phone backup. Overall it does less than Immich while asking you to pay for some of it.

### [darktable](https://www.darktable.org/)

A superb open-source RAW editor and non-destructive photo processor (GPL-3.0). If your goal is *editing* photos - tone curves, colour grading, masking, noise reduction - darktable is world-class. But it is not really a photo *manager*. It has no meaningful face recognition (only via a community Lua script), no semantic search, and minimal duplicate detection. It's the wrong tool for this job, though it pairs well with a dedicated catalogue tool.

### [XnView MP](https://www.xnview.com/en/xnviewmp/)

A fast, lightweight file browser that reads 500+ image formats. Free for personal use, but it's closed-source freeware - not open source. It has a basic duplicate finder (file-based, not ML) and rudimentary face detection, but no semantic search, no meaningful face recognition, and no web interface. It's excellent for quickly viewing and batch-converting files, but it's a file browser, not a catalogue.

### [Google Photos](https://photos.google.com/)

The benchmark for AI-powered photo search. Google's semantic search is genuinely best-in-class - natural language queries like "dog on a beach at sunset" just work. Face recognition is excellent, including pets. Mobile backup is seamless. And 15GB is free.

The price is your privacy. Your photos live on Google's servers, processed by Google's AI, governed by Google's privacy policy, under US jurisdiction. The 15GB free tier is shared with Gmail and Drive. Advanced editing features increasingly require a Google One subscription. You don't control your data, and Google has a track record of shutting down services. If criteria (d) and (e) from my list above matter to you at all, this is a non-starter.

### [Apple Photos](https://www.apple.com/photos/)

Apple's answer to the same problem, and arguably the best *consumer* photo management experience. On-device face recognition and semantic search powered by Apple's ML frameworks - your photos are analysed locally, not in the cloud. Duplicate detection is built in. The UI is clean and polished.

But it only exists within Apple's ecosystem - no Windows, no Linux. It's closed-source and proprietary. Organisational features are shallow compared to any dedicated tool (no meaningful tagging, limited metadata). iCloud free tier is a miserly 5GB. And if you're not already in the Apple ecosystem, it's simply not an option.

### [Adobe Bridge](https://www.adobe.com/products/bridge.html)

Worth mentioning because it's genuinely free (no subscription required) and has excellent metadata/XMP management. It's a professional file browser with good RAW support via Camera Raw. But it has no face recognition, no duplicate detection, no semantic search, no Linux support, and collects Adobe telemetry. It's a file browser for Adobe users, not a photo catalogue.

## NAS-Bundled Photo Management

There is a third camp that does not fit neatly into either of the above: photo management apps that ship with network-attached storage (NAS) devices. You have already bought the hardware, so the software is "free" in a sense - but it is proprietary, closed-source, and locked to the vendor's ecosystem. You cannot run Synology Photos on a QNAP, or QuMagie on a Synology. If you outgrow the software or the vendor stops developing it, your only option is to start over with something else on the same box (most NAS devices can run Docker, so Immich and PhotoPrism are popular escape hatches).

The privacy story is genuinely good, though - all processing happens on your NAS, your data stays on your network, and there is no cloud dependency for the AI features. And if you already own a compatible NAS, the barrier to entry is essentially zero.

### [Synology Photos](https://www.synology.com/en-global/dsm/feature/photos)

The most mature NAS photo solution, and it shows. Synology Photos is bundled with DSM 7 and provides a clean web UI, solid mobile apps for iOS and Android, and good multi-user support with separate Personal and Shared spaces. Face recognition and object/scene detection ("Subjects" albums) work well, and conditional (smart) albums are robust and multi-criteria. RAW support covers 23+ formats. All AI processing runs locally on the NAS hardware. It feels polished in a way that the competition does not.

The big gap is semantic search - there is none. Search is purely attribute-based: date, camera, tag, person, location. You cannot type "dog on a beach" and get results. Face recognition and object detection are also restricted to specific NAS models (generally higher-end x86 units), so a large portion of Synology's own product line cannot use the headline AI features at all. Duplicate detection is limited to "Similar Stacks" using perceptual hashing within a 12-hour time window - there is no library-wide scan and no similarity levels. If criteria (a) and (c) matter to you, Synology Photos falls short.

### [UGREEN Photos](https://nas.ugreen.com/)

The most ambitious NAS photo offering on AI features, and the newest. UGREEN's iDX series (Intel Core Ultra 7 with NPU) provides genuine natural-language semantic search, OCR for text within images, and even user-trainable object categories - features neither Synology nor QNAP offer. The NPU hardware acceleration is a real differentiator for on-device ML. Privacy is excellent: everything runs locally, cloud services are entirely optional.

The catch is maturity. UGOS Pro is significantly younger than DSM or QTS, and users report clunky translations, slow uploads, and a Photos app that feels disorganised. The full AI stack is locked to the expensive iDX hardware - cheaper UGREEN models get substantially less. Duplicate detection is mentioned in the marketing but poorly documented. It is the one to watch, but today it feels more like a promising beta than a finished product.

### [QNAP QuMagie](https://www.qnap.com/en-us/software/qumagie)

QNAP's answer to AI photo management, with semantic search added in April 2025 (v2.6.0). It has face recognition, object/scene detection ("Things" albums), and optional hardware AI accelerators (Google Coral TPU, QNAP's own M.2/USB modules) to speed up processing. Multi-user support with per-user and per-group permissions is well thought out. All processing is local via QNAP AI Core.

The problems are significant. Face recognition is widely criticised for quality - faces fragment into many tiny clusters for the same person, and correcting mistakes does not trigger re-analysis of unmatched faces. Worse, all face tags live only in the database, not in image files. If QuMagie reindexes - which can happen after updates, reboots, or by accident - all your manual tagging work is lost. Users on QNAP's forums report losing tens of hours of careful face identification this way. Smart albums are present but buggy (tagged-people albums sometimes display as empty). HEIC support requires a separate paid licence, which feels miserly. And performance reportedly degrades badly around 90,000 images. Semantic search requires x86 hardware with at least 8GB RAM, excluding ARM-based models.

## Comparison Table

The five key criteria from above: **(a)** semantic search, **(b)** face recognition, **(c)** duplicate/similarity detection, **(d)** data privacy, **(e)** free/affordable.

| | Photonarium | digiKam | Immich | PhotoPrism | darktable | XnView MP | Google Photos | Apple Photos | Adobe Bridge | Synology Photos | UGREEN Photos | QNAP QuMagie |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **License** | Apache-2.0 | GPL-2.0 | AGPL-3.0 | AGPL-3.0 (features paywalled) | GPL-3.0 | Freeware (closed) | Proprietary | Proprietary | Proprietary (free) | Proprietary (bundled) | Proprietary (bundled) | Proprietary (bundled) |
| **Platforms** | Win/Mac/Linux | Win/Mac/Linux | Server + web + mobile | Server + web (PWA) | Win/Mac/Linux | Win/Mac/Linux | Web + mobile | Apple only | Win/Mac | NAS + web + mobile | NAS + web + mobile | NAS + web + mobile |
| **Fully offline** | Yes | Yes | Yes (self-hosted) | Yes (self-hosted) | Yes | Yes | No | Hybrid | Yes | Yes | Yes | Yes |
| **(a) Semantic search** | Yes (CLIP) | Planned/partial | Yes (CLIP)\* | Yes (TF2 + optional LLM)\* | No | No | Yes (best-in-class)\* | Yes (on-device)\* | No | No | Yes (iDX only)\* | Yes (x86 + 8GB)\* |
| **(b) Face recognition** | Yes | Yes | Yes | Yes | No | Basic | Yes | Yes (on-device) | No | Yes (model-restricted) | Yes (iDX only) | Yes (fragile) |
| **(c) Duplicate detection** | Yes (4 levels) | Yes (perceptual hash) | Yes (ML-based) | Basic (checksums only) | Minimal | File-based | Basic | Yes | No | Basic (time-windowed) | Basic | Basic (no workflow) |
| **(d) Data stays local** | Yes | Yes [1] | Mostly [2] | Mostly [3] | Yes [4] | Mostly [5] | No | No | No | Yes | Yes | Yes |
| **(e) Truly free** | Yes | Yes | Yes | Partially (paywalled features) | Yes | Personal use only | 15GB free tier | Bundled with hardware | Yes | Bundled with NAS | Bundled with NAS | Mostly (HEIC paywalled) |
| **Image captioning** | Yes (BLIP/BLIP-2) | No | No | Optional (external LLM) | No | No | Yes | Yes | No | No | No | No |
| **Web-based UI** | Yes | No | Yes | Yes | No | No | Yes | Limited | No | Yes | Yes | Yes |
| **Phone backup** | No | No | Yes | No | No | No | Yes | Yes | No | Yes | Yes | Yes |
| **Multi-user** | No | No | Yes | Paid tier | No | No | Yes | Yes (Family) | No | Yes | Yes | Yes |
| **Install complexity** | Low (Python + pip) | Medium (KDE) | Medium-high (Docker) | Medium-high (Docker) | Low-medium | Low | None (cloud) | None (bundled) | Low | None (bundled) | None (bundled) | None (bundled) |
| **RAW support** | Good | Excellent | Good | Good | Excellent | Excellent | Good | Good | Excellent | Good | Good | Good |
| **Non-destructive editing** | No | Yes | Basic | Yes | No | No | Yes | Yes | No | No | No | No |

\* These apps offer semantic search but do not support negative terms (e.g. "beach -sunset") to exclude concepts from results. Photonarium does.

[1] digiKam: core library is local; map/geolocation views may use external map/tile services.

[2] Immich: core is self-hosted/local; some features commonly pull external map tiles and some deployments need pre-seeded ML assets to be fully offline.

[3] PhotoPrism: core is local; Places (maps/reverse geocoding) typically relies on external services unless disabled.

[4] darktable: core is local; the map module uses external map providers if you enable it.

[5] XnView MP: local file manager, but commonly phones home for update checks unless you disable it.

### Where Photonarium Fits

Photonarium occupies a niche that none of the above quite covers: a lightweight, fully offline desktop tool that combines CLIP semantic search, face detection and recognition, multi-level duplicate detection, and BLIP image captioning - all accessible via a browser-based UI, without requiring Docker infrastructure, a database server, KDE frameworks, a cloud account, or specific NAS hardware. It is the simplest install of any AI-powered option (just Python and pip), and it runs on Windows, Mac, and Linux with zero telemetry under a permissive Apache-2.0 license.

The NAS-bundled tools are worth a special mention because they get the privacy story right and the barrier to entry is low if you already own the hardware. But their AI features are typically restricted to specific (expensive) models, their duplicate detection is shallow, none of them offer image captioning or quality scoring, and you are locked into a single vendor's closed ecosystem with no ability to fix or extend anything. If the vendor decides your NAS model is end-of-life, the software stops improving.

The trade-offs are: no automatic phone backup (the web UI works fine on mobile browsers, but it cannot sync your camera roll in the background the way a native app from Google, Apple, Immich, or a NAS vendor can), no multi-user support, and no non-destructive editing. It is also new so has a much smaller community than the established projects! But if what you want is to point a tool at your photo folders and immediately start searching them semantically, finding duplicates, and tagging faces - all without sending a single byte off your machine - Photonarium is designed for exactly that.

## The Great 'AI' Debate

I made a deliberate decision at the start of creating Photonarium: I would wear the hats of visionary, UX designer, architect, project manager, and tester. I would see if AI (more precisely, an LLM) could be the software developer.

Why? I'm an expert software engineer and technical project leader. I've written software for eons and I consider myself to have been engaged with the LLM revolution since the [OpenAI Playground](https://platform.openai.com/docs/overview) was first announced. I've been using LLMs throughout and had many ups and downs. I'm acutely aware how deeply divided the software development community is around AI: on the one side, you have the nay-sayers who claim "it's not *really* AI", and "it's just a bubble", and "it doesn't understand so it can't write real software", and "I haven't got time to waste on that" etc. On the other side, you have the AI zealots who say "in the future we won't need programmers", and "vibe coding is the next big thing", etc.

I would position myself as a pragmatist. Based upon my experience, LLMs (especially when encased in additional tooling to help with coding tasks), can be useful. I've been saying for years that using an LLM to assist software development is like working with the most patient, the most widely experienced, and the fastest software engineer you've ever met. But they have concussion. Keep that last fact in mind, and you might make progress.

Recently, I'd been using [Anthropic's Claude Code](https://code.claude.com/docs/en/overview) and I've been generally impressed. For this project, I chose to use it to write the software, do quite a bit of the UI (and a little of the UX) design, and even some of the testing. For the most part, I didn't even *look* at the code, let alone write it. I did have to dive in from time to time, but that was very much the exception rather than the rule.

Overall, I hope that Photonarium speaks for itself. I believe it's well documented, the code is decently commented and reasonably well structured, it's functionally good and absolutely achieves (and surpasses) all of the goals I had in mind when I started. And it took two weeks. My sense of the LLM having concussion has reduced to a sense of it being mildly forgetful and occasionally making dumb (e.g. performance, architectural, duplicative code) decisions. But I've led a many software teams over the years and those issues are not unique to LLMs!

If I weren't an expert software engineer, and an experienced project leader, and I didn't have strong instincts honed over years of experience as to the potential reasons why something isn't working the way it should, creating Photonarium or any similarly complex program using an LLM would be *impossible*. No question. And I don't think this is going to change for the foreseeable future, not even two more papers down the line... I absolutely wouldn't trust an LLM to write critical (e.g. life-safety) code, nor do I believe they would write something highly complex, like a compiler for a language where the 'specification' is a 600-page Reference Guide and a reference implementation in dense AArch32 Arm assembly language. Not a snowball in Hell's chance. For that we'd need to layer *true* AGI on top of (or in place of) the LLM.

But, I hope Photonarium *does* prove that LLMs are now good enough to be a valuable tool, amongst many other tools, for software developers to work smarter and faster. I genuinely felt excited during this development to know I could have an idea for a new feature, think about how to describe it clearly and concisely, and a few minutes later be testing the finished implementation...
