/* ====================================================================
   Photonarium Landing Page — Script
   Minimal JS for carousel navigation, smooth scroll, and mobile menu.
   ==================================================================== */

(function () {
    'use strict';

    /* ----------------------------------------------------------------
       Mobile navigation toggle
       ---------------------------------------------------------------- */
    const navToggle = document.getElementById('navToggle');
    const navLinks  = document.getElementById('navLinks');

    if (navToggle && navLinks) {
        navToggle.addEventListener('click', () => {
            navToggle.classList.toggle('active');
            navLinks.classList.toggle('open');
        });

        /* Close mobile menu when a nav link is clicked */
        navLinks.addEventListener('click', (e) => {
            if (e.target.closest('a[href^="#"]')) {
                navToggle.classList.remove('active');
                navLinks.classList.remove('open');
            }
        });
    }

    /* ----------------------------------------------------------------
       Carousel arrow navigation
       ---------------------------------------------------------------- */
    const carousel = document.getElementById('carousel');
    const leftBtn  = document.getElementById('carouselLeft');
    const rightBtn = document.getElementById('carouselRight');

    if (carousel && leftBtn && rightBtn) {
        /** Scroll the carousel by one slide width */
        function scrollCarousel(direction) {
            const slide = carousel.querySelector('.carousel-slide');
            if (!slide) return;
            /* Scroll by the slide width + gap (read from computed style) */
            const gap = parseFloat(getComputedStyle(carousel).gap) || 32;
            const distance = slide.offsetWidth + gap;
            carousel.scrollBy({ left: direction * distance, behavior: 'smooth' });
        }

        leftBtn.addEventListener('click',  () => scrollCarousel(-1));
        rightBtn.addEventListener('click', () => scrollCarousel(1));
    }

    /* ----------------------------------------------------------------
       Smooth-scroll for anchor links (fallback for browsers without
       CSS scroll-behavior support)
       ---------------------------------------------------------------- */
    document.querySelectorAll('a[href^="#"]').forEach((link) => {
        link.addEventListener('click', (e) => {
            const targetId = link.getAttribute('href');
            if (targetId === '#') return;
            const target = document.querySelector(targetId);
            if (target) {
                e.preventDefault();
                target.scrollIntoView({ behavior: 'smooth' });
            }
        });
    });

    /* ----------------------------------------------------------------
       Query-string scroll — use e.g. ?support to scroll to #support
       on page load. Bypasses Chrome's broken native fragment scrolling
       which ignores scroll-margin-top and can't be reliably overridden.
       ---------------------------------------------------------------- */
    const section = location.search.slice(1);
    if (section && document.getElementById(section)) {
        /**
         * Polls until document height stabilises (lazy images done loading),
         * then scrolls to the bottom. Two consecutive reads 200ms apart must
         * agree before we consider the layout settled.
         */
        const target = document.getElementById(section);

        function scrollWhenStable() {
            let lastHeight = 0;
            const poll = setInterval(() => {
                const height = document.body.scrollHeight;
                if (height === lastHeight) {
                    clearInterval(poll);
                    target.scrollIntoView();
                }
                lastHeight = height;
            }, 200);
        }

        if (document.hidden) {
            document.addEventListener('visibilitychange', function onVisible() {
                if (!document.hidden) {
                    document.removeEventListener('visibilitychange', onVisible);
                    scrollWhenStable();
                }
            });
        } else {
            scrollWhenStable();
        }
    }

    /* ----------------------------------------------------------------
       Floating "Photo" translations in hero background
       Faint words in many scripts drift continuously across the hero,
       reinforcing the international nature of the app.  Each word has
       a fixed velocity; when it drifts fully off-screen it respawns
       at the diagonally opposite edge so it re-enters naturally
       without changing direction or speed (no popping).
       ---------------------------------------------------------------- */
    const hero = document.getElementById('hero');
    const motionOk = !window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    if (hero && motionOk) {
        const words = [
            '写真', 'Фото', '사진', '照片', 'صورة', 'फ़ोटो', 'ছবি',
            'รูปถ่าย', 'ảnh', 'φωτογραφία', 'תמונה', 'фотографія',
            'ფოტო', 'снимка', 'zdjęcie', 'fotografie', 'fotó', 'foto',
            'bild', 'kuva', 'imagen', 'resim', 'снимок', '相片', 'фото',
            '光', '影像', 'valokuva', 'ljósmynd', 'picha',
            'Photo', 'Photograph',
        ];

        /** Random float in [min, max) rounded to two decimal places */
        function randF(min, max) {
            return +(Math.random() * (max - min) + min).toFixed(2);
        }

        const container = document.createElement('div');
        container.className = 'hero-words';
        container.setAttribute('aria-hidden', 'true');

        /* Build spans and item state (positions deferred until first layout) */
        const items = words.map((word) => {
            const span = document.createElement('span');
            span.textContent = word;
            span.style.fontSize = `${randF(1.4, 2.6)}rem`;
            span.style.opacity = `${randF(0.08, 0.16)}`;
            container.appendChild(span);

            /* Random drift direction (full 360°) at 8–18 px/s */
            const angle = Math.random() * Math.PI * 2;
            const speed = randF(8, 18);

            return { el: span, x: 0, y: 0, w: 0, h: 0,
                vx: +(Math.cos(angle) * speed).toFixed(2),
                vy: +(Math.sin(angle) * speed).toFixed(2) };
        });

        hero.insertBefore(container, hero.firstChild);

        let dismissed = false;
        let resizeObserver = null;

        /* Dismiss on first user interaction — stop loop, fade out, remove */
        function dismiss() {
            dismissed = true;
            if (resizeObserver) resizeObserver.disconnect();
            container.classList.add('dismissed');
            container.addEventListener('transitionend', () => container.remove());
        }
        ['scroll', 'click', 'keydown', 'touchstart'].forEach((evt) =>
            window.addEventListener(evt, dismiss, { once: true, passive: true }),
        );

        /* --- First layout: cache sizes, scatter, start RAF loop -------- */
        requestAnimationFrame(() => {
            if (dismissed) return;

            let W = container.offsetWidth;
            let H = container.offsetHeight;

            /* Cache element sizes and scatter initial positions */
            items.forEach((item) => {
                const rect = item.el.getBoundingClientRect();
                item.w = rect.width;
                item.h = rect.height;
                item.x = (0.05 + Math.random() * 0.85) * W;
                item.y = (0.05 + Math.random() * 0.85) * H;
                item.el.style.transform =
                    `translate(${item.x.toFixed(1)}px, ${item.y.toFixed(1)}px)`;
            });

            /* Track container size without per-frame layout reads */
            resizeObserver = new ResizeObserver((entries) => {
                for (const entry of entries) {
                    W = entry.contentRect.width;
                    H = entry.contentRect.height;
                }
            });
            resizeObserver.observe(container);

            let lastTime = performance.now();

            function tick(now) {
                if (dismissed) return;

                /* Cap dt to avoid huge jumps after a background-tab resume */
                const dt = Math.min((now - lastTime) / 1000, 0.1);
                lastTime = now;

                for (let i = 0; i < items.length; i++) {
                    const it = items[i];
                    it.x += it.vx * dt;
                    it.y += it.vy * dt;

                    /* Wrap each axis independently, only checking the edge
                       the word is heading toward.  Without the direction
                       guard a freshly-respawned word (sitting just off the
                       opposite edge) would immediately trigger the other
                       boundary check and ping-pong forever. */
                    if (it.vx > 0 && it.x > W)           it.x = -(it.w + Math.random() * 60);
                    else if (it.vx < 0 && it.x + it.w < 0) it.x = W + Math.random() * 60;
                    if (it.vy > 0 && it.y > H)            it.y = -(it.h + Math.random() * 60);
                    else if (it.vy < 0 && it.y + it.h < 0) it.y = H + Math.random() * 60;

                    it.el.style.transform =
                        `translate(${it.x.toFixed(1)}px, ${it.y.toFixed(1)}px)`;
                }

                requestAnimationFrame(tick);
            }

            requestAnimationFrame(tick);
        });
    }

})();
