import {useEffect, useRef} from 'react';

/**
 * Adds the `revealed` class to every `.reveal` element inside the returned ref
 * once it scrolls into view. Any section that uses the `reveal` class must
 * attach this ref to its root, or its content stays at opacity 0.
 */
export default function useScrollReveal<T extends HTMLElement = HTMLDivElement>() {
  const ref = useRef<T>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (typeof IntersectionObserver === 'undefined') {
      el.querySelectorAll('.reveal').forEach((t) => t.classList.add('revealed'));
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.classList.add('revealed');
          }
        });
      },
      {threshold: 0.1, rootMargin: '0px 0px -50px 0px'},
    );
    el.querySelectorAll('.reveal').forEach((t) => observer.observe(t));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return ref;
}
