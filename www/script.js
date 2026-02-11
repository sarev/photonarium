/* ====================================================================
   Imaginary Landing Page — Script
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

})();
