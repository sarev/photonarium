# DISCLAIMER

This document is opinionated, but hopefully fair!

# Background

The motivation behind creating Imaginary was born of looking at the pre-existing solutions and coming away feeling dissatisfied. For example, your options broadly divide into two camps:

1. Commercial
2. Free

I was looking for some key features:

a) Be able to search through my images semantically

b) Be able to find specific people (ideally with automated face tagging)

c) Be able to easily find duplicates and near duplicates, groups of related images

d) Not have my privacy invaded

e) Not have to shell out loads of money!

## The Commercial Horror Show

The commercial offerings might be functionally rich, but they are either expensive (high up-front cost), or worse still, adopt a subscription model where you have to keep paying indefinitely for the privilege of looking at your own photos. In addition, these apps are increasingly pushing towards being 'cloud' based, so your images are leaving your personal possession to be stored 'somewhere'. Generally, these offerings come from US-based corporations operating under the famously weak US privacy laws (compared to the EU) where consumer protection is an afterthought. They come with massive, impenetrable licences that you **must** accept in order to use the app, and they often hoover up your personal usage/performance data for who knows what (typically to "improve the services they and their partners offer"). In addition to all this, they are closed-source so if you would like to see a new feature or a change, at best you might be able to raise a ticket somewhere and hope, one day, that they choose to pay attention to it. No thanks!

## The Free Landscape

There are a number of worthy, free apps out there for managing your images. Here's a (hopefully!) honest assessment of how those projects compare to Imaginary, as of late-2025/early-2026:

### [digiKam](https://www.digikam.org/)

The heavyweight champion of open-source photo management. It's a KDE project licensed under GPL-2.0, runs on Windows, Mac, and Linux, works fully offline, and collects zero telemetry. It has face detection and recognition using deep learning, duplicate detection via perceptual hashing, extensive RAW support (1260+ camera formats), non-destructive editing, and a staggering breadth of metadata tools. It's the most feature-complete free desktop photo manager by a wide margin.

The downsides? The UI is dense and technical — very much a power-user tool with a KDE aesthetic that can feel overwhelming. It's a large download due to bundled KDE frameworks. And critically, it has no CLIP-style semantic search yet (the team is exploring it, but as of late 2025 it's still partial/planned). No web UI, no mobile interface. Face training requires a fair bit of manual effort up front.

### [Immich](https://immich.app/)

The darling of the self-hosted community and the closest thing to a free Google Photos replacement. Licensed under AGPL-3.0, it's a server application with a polished web UI and native iOS/Android apps with automatic photo backup. It uses CLIP for semantic search, InsightFace for face recognition, and has ML-based duplicate detection. Multi-user support, shared albums, map views — it's impressively complete for a project that only reached stable v2.0 in October 2025.

The catch is that it's a *server*, not a desktop app. You need Docker, at least 6-8GB of RAM for the ML processing, and ideally a Linux host (Windows/macOS via Docker Desktop is supported but discouraged). If you're comfortable running infrastructure, it's excellent. If you just want to point something at your photo folders and go, it's overkill.

### [PhotoPrism](https://www.photoprism.app/)

A more established self-hosted alternative to Immich, also AGPL-3.0. It has AI-powered classification (recently upgraded to TensorFlow 2), face recognition, a decent web PWA, and optional integration with Ollama/OpenAI for captioning. Runs via Docker, self-hosted, no telemetry.

However, some features are paywalled behind expensive paid memberships (Essentials ~€200/year, Plus ~€600/year) — including multi-user management, which feels restrictive for an AGPL project. Its duplicate detection is basic (exact checksums only, no visual similarity browsing). No native mobile app, no phone backup. Overall it does less than Immich while asking you to pay for some of it.

### [darktable](https://www.darktable.org/)

A superb open-source RAW editor and non-destructive photo processor (GPL-3.0). If your goal is *editing* photos — tone curves, colour grading, masking, noise reduction — darktable is world-class. But it is not really a photo *manager*. It has no meaningful face recognition (only via a community Lua script), no semantic search, and minimal duplicate detection. It's the wrong tool for this job, though it pairs well with a dedicated catalogue tool.

### [XnView MP](https://www.xnview.com/en/xnviewmp/)

A fast, lightweight file browser that reads 500+ image formats. Free for personal use, but it's closed-source freeware — not open source. It has a basic duplicate finder (file-based, not ML) and rudimentary face detection, but no semantic search, no meaningful face recognition, and no web interface. It's excellent for quickly viewing and batch-converting files, but it's a file browser, not a catalogue.

### [Google Photos](https://photos.google.com/)

The benchmark for AI-powered photo search. Google's semantic search is genuinely best-in-class — natural language queries like "dog on a beach at sunset" just work. Face recognition is excellent, including pets. Mobile backup is seamless. And 15GB is free.

The price is your privacy. Your photos live on Google's servers, processed by Google's AI, governed by Google's privacy policy, under US jurisdiction. The 15GB free tier is shared with Gmail and Drive. Advanced editing features increasingly require a Google One subscription. You don't control your data, and Google has a track record of shutting down services. If criteria (d) and (e) from my list above matter to you at all, this is a non-starter.

### [Apple Photos](https://www.apple.com/photos/)

Apple's answer to the same problem, and arguably the best *consumer* photo management experience. On-device face recognition and semantic search powered by Apple's ML frameworks — your photos are analysed locally, not in the cloud. Duplicate detection is built in. The UI is clean and polished.

But it only exists within Apple's ecosystem — no Windows, no Linux. It's closed-source and proprietary. Organisational features are shallow compared to any dedicated tool (no meaningful tagging, limited metadata). iCloud free tier is a miserly 5GB. And if you're not already in the Apple ecosystem, it's simply not an option.

### [Adobe Bridge](https://www.adobe.com/products/bridge.html)

Worth mentioning because it's genuinely free (no subscription required) and has excellent metadata/XMP management. It's a professional file browser with good RAW support via Camera Raw. But it has no face recognition, no duplicate detection, no semantic search, no Linux support, and collects Adobe telemetry. It's a file browser for Adobe users, not a photo catalogue.

## Comparison Table

The five key criteria from above: **(a)** semantic search, **(b)** face recognition, **(c)** duplicate/similarity detection, **(d)** privacy, **(e)** free/affordable.

| | Imaginary | digiKam | Immich | PhotoPrism | darktable | XnView MP | Google Photos | Apple Photos | Adobe Bridge |
|---|---|---|---|---|---|---|---|---|---|
| **License** | Apache-2.0 | GPL-2.0 | AGPL-3.0 | AGPL-3.0 (features paywalled) | GPL-3.0 | Freeware (closed) | Proprietary | Proprietary | Proprietary (free) |
| **Platforms** | Win/Mac/Linux | Win/Mac/Linux | Server + web + mobile | Server + web (PWA) | Win/Mac/Linux | Win/Mac/Linux | Web + mobile | Apple only | Win/Mac |
| **Fully offline** | Yes | Yes | Yes (self-hosted) | Yes (self-hosted) | Yes | Yes | No | Hybrid | Yes |
| **(a) Semantic search** | Yes (CLIP) | Planned/partial | Yes (CLIP)\* | Yes (TF2 + optional LLM)\* | No | No | Yes (best-in-class)\* | Yes (on-device)\* | No |
| **(b) Face recognition** | Yes | Yes | Yes | Yes | No | Basic | Yes | Yes (on-device) | No |
| **(c) Duplicate detection** | Yes (4 levels) | Yes (perceptual hash) | Yes (ML-based) | Basic (checksums only) | Minimal | File-based | Basic | Yes | No |
| **(d) Privacy (no telemetry)** | Yes | Yes | Yes | Yes | Yes | Yes | No | Mostly | No |
| **(e) Truly free** | Yes | Yes | Yes | Partially (paywalled features) | Yes | Personal use only | 15GB free tier | Bundled with hardware | Yes |
| **Image captioning** | Yes (BLIP/BLIP-2) | No | No | Optional (external LLM) | No | No | Yes | Yes | No |
| **Web-based UI** | Yes | No | Yes | Yes | No | No | Yes | Limited | No |
| **Mobile app** | No | No | Yes | No | No | No | Yes | Yes | No |
| **Multi-user** | No | No | Yes | Paid tier | No | No | Yes | Yes (Family) | No |
| **Install complexity** | Low (Python + pip) | Medium (KDE) | Medium-high (Docker) | Medium-high (Docker) | Low-medium | Low | None (cloud) | None (bundled) | Low |
| **RAW support** | Good | Excellent | Good | Good | Excellent | Excellent | Good | Good | Excellent |
| **Non-destructive editing** | No | Yes | Basic | Yes | No | No | Yes | Yes | No |

\* These apps offer semantic search but do not support negative terms (e.g. "beach -sunset") to exclude concepts from results. Imaginary does.

### Where Imaginary Fits

Imaginary occupies a niche that none of the above quite covers: a lightweight, fully offline desktop tool that combines CLIP semantic search, face detection and recognition, multi-level duplicate detection, and BLIP image captioning — all accessible via a browser-based UI, without requiring Docker infrastructure, a database server, KDE frameworks, or a cloud account. It's the simplest install of any AI-powered option (just Python and pip), and it runs on Windows, Mac, and Linux with zero telemetry under a permissive Apache-2.0 license.

The trade-offs are: no mobile apps or phone backup, no multi-user support, and no non-destructive editing. It's also new so has a much smaller community than the established projects! But if what you want is to point a tool at your photo folders and immediately start searching them semantically, finding duplicates, and tagging faces — all without sending a single byte off your machine — Imaginary is designed for exactly that.

## The Great 'AI' Debate

I made a deliberate decision at the start of creating Imaginary: I would wear the hats of visionary, UX designer, architect, project manager, and tester. I would see if AI (more precisely, an LLM) could be the software developer.

Why? I'm an expert software engineer and technical project leader. I've written software for eons and I consider myself to have been engaged with the LLM revolution since the [OpenAI Playground](https://platform.openai.com/docs/overview) was first announced. I've been using LLMs throughout and had many ups and downs. I'm acutely aware how deeply divided the software development community is around AI: on the one side, you have the nay-sayers who claim "it's not *really* AI", and "it's just a bubble", and "it doesn't understand so it can't write real software", and "I haven't got time to waste on that" etc. On the other side, you have the AI zealots who say "in the future we won't need programmers", and "vibe coding is the next big thing", etc.

I would position myself as a pragmatist. Based upon my experience, LLMs (especially when encased in additional tooling to help with coding tasks), can be useful. I've been saying for years that using an LLM to assist software development is like working with the most patient, the most widely experienced, and the fastest software engineer you've ever met. But they have concussion. Keep that last fact in mind, and you might make progress.

Recently, I'd been using [Anthropic's Claude Code](https://code.claude.com/docs/en/overview) and I've been generally impressed. For this project, I chose to use it to write the software, do quite a bit of the UI (and a little of the UX) design, and even some of the testing. For the most part, I didn't even *look* at the code, let alone write it. I did have to dive in from time to time, but that was very much the exception rather than the rule.

Overall, I hope that Imaginary speaks for itself. I believe it's well documented, the code is decently commented and reasonably well structured, it's functionally good and absolutely achieves (and surpasses) all of the goals I had in mind when I started. And it took two weeks. My sense of the LLM having concussion has reduced to a sense of it being mildly forgetful and occasionally making dumb (e.g. performance, architectural, duplicative code) decisions. But I've led a many software teams over the years and those issues are not unique to LLMs!

If I weren't an expert software engineer, and an experienced project leader, and I didn't have strong instincts honed over years of experience as to the potential reasons why something isn't working the way it should, creating Imaginary or any similarly complex program using an LLM would be *impossible*. No question. And I don't think this is going to change for the foreseeable future, not even two more papers down the line... I absolutely wouldn't trust an LLM to write critical (e.g. life-safety) code, nor do I believe they would write something highly complex, like a compiler for a language where the 'specification' is a 600-page Reference Guide and a reference implementation in dense AArch32 Arm assembly language. Not a snowball in Hell's chance. For that we'd need to layer *true* AGI on top of (or in place of) the LLM.

But, I hope Imaginary *does* prove that LLMs are now good enough to be a valuable tool, amongst many other tools, for software developers to work smarter and faster. I genuinely felt excited during this development to know I could have an idea for a new feature, think about how to describe it clearly and concisely, and a few minutes later be testing the finished implementation...
