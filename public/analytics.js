/* Lightweight, privacy-conscious GA4 instrumentation for Louisiana911. */
(() => {
  'use strict';

  if (window.__louisiana911AnalyticsLoaded) return;
  window.__louisiana911AnalyticsLoaded = true;

  const MEASUREMENT_ID = 'G-WHHVE8V5DW';
  const STARTED_AT = performance.now();
  const PAGE_TYPE = getPageType();
  const SCROLL_MILESTONES = [10, 25, 50, 75, 90, 100];
  const TIME_MILESTONES = [15, 30, 60, 120, 300, 600];
  const SCROLL_CONTAINER_SELECTOR = '.incident-list, .info-content, .history-controls, .date-picker, [data-analytics-scroll]';
  const sentScrollMilestones = new Set();
  const sentTimeMilestones = new Set();
  const seenSections = new Set();
  const containerScrollStates = new Map();
  const pendingScrollContainers = new WeakSet();
  let activeSeconds = 0;
  let clickCount = 0;
  let maxScrollDepth = 0;
  let scrollFramePending = false;
  let lastPointerType = 'unknown';
  let pageSummarySent = false;
  let vitalsSent = false;
  let largestContentfulPaint = 0;
  let cumulativeLayoutShift = 0;
  let interactionToNextPaint = 0;

  window.dataLayer = window.dataLayer || [];
  window.gtag = window.gtag || function gtag() {
    window.dataLayer.push(arguments);
  };

  window.gtag('js', new Date());
  window.gtag('config', MEASUREMENT_ID, {
    allow_google_signals: false,
    allow_ad_personalization_signals: false,
    page_type: PAGE_TYPE,
    source_view: getSourceView(),
    page_location: safePageUrl(location.href, true),
    page_referrer: safePageUrl(document.referrer),
    page_title: PAGE_TYPE === 'shared_incident' ? 'Shared incident | Louisiana911' : document.title,
    transport_type: 'beacon'
  });

  // Public hook for richer feature-specific events without another dependency.
  window.louisiana911Analytics = Object.freeze({
    track: (name, parameters = {}) => track(name, parameters)
  });

  loadGoogleTag();
  observeWebVitals();

  document.addEventListener('pointerdown', (event) => {
    lastPointerType = event.pointerType || 'unknown';
  }, { capture: true, passive: true });

  document.addEventListener('click', trackClick, { capture: true, passive: true });
  document.addEventListener('change', trackControlChange, { capture: true, passive: true });
  document.addEventListener('scroll', trackContainerScroll, { capture: true, passive: true });
  window.addEventListener('scroll', scheduleScrollCheck, { passive: true });
  window.addEventListener('load', () => {
    scheduleScrollCheck();
    scheduleIdle(reportPagePerformance);
    observeSections();
  }, { once: true });

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') {
      reportWebVitals();
      reportPageSummary();
    }
  });
  window.addEventListener('pagehide', () => {
    reportWebVitals();
    reportPageSummary();
  }, { capture: true });

  window.addEventListener('error', (event) => {
    track('client_error', {
      error_type: 'javascript',
      error_name: cleanToken(event.error?.name || 'Error', 40),
      source_file: safeFileName(event.filename),
      line_number: Number(event.lineno) || 0
    });
  }, { passive: true });

  window.addEventListener('unhandledrejection', (event) => {
    track('client_error', {
      error_type: 'unhandled_promise',
      error_name: cleanToken(event.reason?.name || typeof event.reason, 40)
    });
  }, { passive: true });

  setInterval(() => {
    if (document.visibilityState !== 'visible') return;
    activeSeconds += 5;
    TIME_MILESTONES.forEach((seconds) => {
      if (activeSeconds < seconds || sentTimeMilestones.has(seconds)) return;
      sentTimeMilestones.add(seconds);
      track('engagement_milestone', { active_seconds: seconds });
    });
  }, 5000);

  function loadGoogleTag() {
    // Local previews retain the event queue for testing without polluting GA4.
    if (!['louisiana911.com', 'www.louisiana911.com'].includes(location.hostname)) return;
    const script = document.createElement('script');
    script.async = true;
    script.src = `https://www.googletagmanager.com/gtag/js?id=${encodeURIComponent(MEASUREMENT_ID)}`;
    script.dataset.analyticsLoader = 'ga4';
    document.head.appendChild(script);
  }

  function track(name, parameters = {}) {
    if (typeof name !== 'string' || !/^[a-z][a-z0-9_]{0,39}$/.test(name)) return;
    const eventParameters = {
      page_type: PAGE_TYPE,
      source_view: getSourceView(),
      ...parameters,
      transport_type: 'beacon'
    };
    Object.keys(eventParameters).forEach((key) => {
      if (eventParameters[key] === '' || eventParameters[key] === null || eventParameters[key] === undefined) {
        delete eventParameters[key];
      }
    });
    window.gtag('event', name, eventParameters);
  }

  function trackClick(event) {
    const rawTarget = event.target;
    if (!(rawTarget instanceof Element)) return;

    const target = rawTarget.closest('a, button, input, select, textarea, [role="button"], .incident-card, .leaflet-interactive') || rawTarget;
    const link = target.closest?.('a[href]');
    const mapElement = target.closest?.('#map, .leaflet-container');
    const destination = link ? safeDestination(link.href) : '';
    const outbound = Boolean(link && isOutboundLink(link.href));
    const position = getClickPosition(event);

    clickCount += 1;
    trackFeatureClick(target);
    track('ui_click', {
      action_type: outbound ? 'outbound_link' : getActionType(target, link, mapElement),
      element_name: getElementName(target, link, mapElement),
      page_region: getPageRegion(target, mapElement),
      destination,
      link_domain: link ? safeLinkDomain(link.href) : '',
      pointer_type: lastPointerType,
      x_viewport_bucket: position.xViewport,
      y_viewport_bucket: position.yViewport,
      y_page_bucket: position.yPage
    });
  }

  function trackFeatureClick(target) {
    if (target.disabled || target.getAttribute?.('aria-disabled') === 'true') return;
    const feature = target.closest?.('[data-source], [data-view-mode], [data-tab], [data-filter], [data-urgency], [data-br-agency], [data-laf-unit], [data-basemap]');
    if (!feature) return;
    const data = feature.dataset;
    if (data.source && ['all', 'caddo', 'batonrouge', 'lafayette', 'neworleans', 'lakecharles'].includes(data.source)) {
      track('source_select', { source_view: data.source });
    } else if (['map', 'list'].includes(data.viewMode)) {
      track('view_mode_select', { view_mode: data.viewMode });
    } else if (['live', 'history'].includes(data.tab)) {
      track('incident_tab_select', { view_mode: data.tab });
    } else if (data.basemap) {
      track('basemap_select', { control_value: cleanToken(data.basemap, 40) });
    } else {
      const key = ['filter', 'urgency', 'brAgency', 'lafUnit'].find(key => data[key]);
      if (key) track('incident_filter', { control_name: key, control_value: cleanToken(data[key], 60) });
    }
  }

  function trackControlChange(event) {
    const control = event.target;
    if (!(control instanceof HTMLInputElement || control instanceof HTMLSelectElement || control instanceof HTMLTextAreaElement)) return;

    const parameters = {
      control_name: getElementName(control),
      control_type: cleanToken(control.type || control.tagName.toLowerCase(), 30),
      page_region: getPageRegion(control)
    };

    if (control instanceof HTMLInputElement && (control.type === 'checkbox' || control.type === 'radio')) {
      parameters.control_state = control.checked ? 'on' : 'off';
    } else if (control instanceof HTMLSelectElement) {
      parameters.control_value = cleanToken(control.value, 60);
    }

    track('control_change', parameters);
  }

  function scheduleScrollCheck() {
    if (scrollFramePending) return;
    scrollFramePending = true;
    requestAnimationFrame(() => {
      scrollFramePending = false;
      const root = document.documentElement;
      const scrollableHeight = Math.max(root.scrollHeight - window.innerHeight, 0);
      if (scrollableHeight === 0) return;
      const depth = Math.min(100, Math.round((window.scrollY / scrollableHeight) * 100));
      maxScrollDepth = Math.max(maxScrollDepth, depth);

      SCROLL_MILESTONES.forEach((milestone) => {
        if (depth < milestone || sentScrollMilestones.has(milestone)) return;
        sentScrollMilestones.add(milestone);
        track('scroll_depth', {
          percent_scrolled: milestone,
          document_height_bucket: bucketPixels(root.scrollHeight)
        });
      });
    });
  }

  function trackContainerScroll(event) {
    const container = event.target;
    if (!(container instanceof Element) || !container.matches(SCROLL_CONTAINER_SELECTOR)) return;
    if (pendingScrollContainers.has(container)) return;
    pendingScrollContainers.add(container);

    requestAnimationFrame(() => {
      pendingScrollContainers.delete(container);
      const scrollableHeight = container.scrollHeight - container.clientHeight;
      if (scrollableHeight < 50) return;

      const depth = Math.min(100, Math.round((container.scrollTop / scrollableHeight) * 100));
      const containerName = getElementName(container);
      const state = containerScrollStates.get(containerName) || new Set();
      containerScrollStates.set(containerName, state);

      SCROLL_MILESTONES.forEach((milestone) => {
        if (depth < milestone || state.has(milestone)) return;
        state.add(milestone);
        track('container_scroll_depth', {
          scroll_container: containerName,
          percent_scrolled: milestone,
          container_height_bucket: bucketPixels(container.scrollHeight)
        });
      });
    });
  }

  function observeSections() {
    if (!('IntersectionObserver' in window)) return;
    const sections = Array.from(document.querySelectorAll('main section, main article, [data-analytics-section]'));
    if (!sections.length) return;

    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting || entry.intersectionRatio < 0.5) return;
        const name = getSectionName(entry.target);
        if (!name || seenSections.has(name)) return;
        seenSections.add(name);
        track('section_view', { section_name: name });
        observer.unobserve(entry.target);
      });
    }, { threshold: 0.5 });

    sections.forEach((section) => observer.observe(section));
  }

  function reportPagePerformance() {
    const navigation = performance.getEntriesByType('navigation')[0];
    if (!navigation) return;
    const firstPaint = performance.getEntriesByName('first-contentful-paint')[0];
    track('page_performance', {
      ttfb_ms: roundedDuration(navigation.responseStart),
      dom_ready_ms: roundedDuration(navigation.domContentLoadedEventEnd),
      load_ms: roundedDuration(navigation.loadEventEnd),
      fcp_ms: roundedDuration(firstPaint?.startTime || 0),
      transfer_kb: Math.round((navigation.transferSize || 0) / 1024),
      navigation_type: cleanToken(navigation.type || 'navigate', 20)
    });
  }

  function observeWebVitals() {
    if (!('PerformanceObserver' in window)) return;
    try {
      new PerformanceObserver((list) => {
        const entries = list.getEntries();
        largestContentfulPaint = entries[entries.length - 1]?.startTime || largestContentfulPaint;
      }).observe({ type: 'largest-contentful-paint', buffered: true });
    } catch (_) { /* Unsupported metric. */ }

    try {
      new PerformanceObserver((list) => {
        list.getEntries().forEach((entry) => {
          if (!entry.hadRecentInput) cumulativeLayoutShift += entry.value;
        });
      }).observe({ type: 'layout-shift', buffered: true });
    } catch (_) { /* Unsupported metric. */ }

    try {
      new PerformanceObserver((list) => {
        list.getEntries().forEach((entry) => {
          if (entry.interactionId && entry.duration > interactionToNextPaint) {
            interactionToNextPaint = entry.duration;
          }
        });
      }).observe({ type: 'event', buffered: true, durationThreshold: 40 });
    } catch (_) { /* Unsupported metric. */ }
  }

  function reportWebVitals() {
    if (vitalsSent) return;
    vitalsSent = true;
    if (largestContentfulPaint) sendWebVital('LCP', Math.round(largestContentfulPaint), rateLcp(largestContentfulPaint));
    sendWebVital('CLS', Math.round(cumulativeLayoutShift * 1000), rateCls(cumulativeLayoutShift));
    if (interactionToNextPaint) sendWebVital('INP', Math.round(interactionToNextPaint), rateInp(interactionToNextPaint));
  }

  function sendWebVital(metricName, metricValue, metricRating) {
    track('web_vital', {
      metric_name: metricName,
      metric_value: metricValue,
      metric_rating: metricRating
    });
  }

  function reportPageSummary() {
    if (pageSummarySent) return;
    pageSummarySent = true;
    scheduleScrollCheck();
    track('page_summary', {
      active_seconds: activeSeconds,
      elapsed_seconds: Math.round((performance.now() - STARTED_AT) / 1000),
      max_scroll_percent: maxScrollDepth,
      click_count: clickCount,
      sections_viewed: seenSections.size
    });
  }

  function getPageType() {
    const path = location.pathname.replace(/\/+$/, '') || '/';
    if (path === '/') return 'incident_map';
    if (path === '/index.html') return 'incident_map';
    if (path.startsWith('/incident/')) return 'shared_incident';
    if (path === '/reports/monthly') return 'monthly_report';
    if (path === '/reports') return 'reports_hub';
    if (path === '/about') return 'about';
    if (path === '/coverage') return 'coverage_hub';
    if (path === '/caddo911' || path.startsWith('/coverage/')) return 'coverage_detail';
    return 'other';
  }

  function getSourceView() {
    const source = document.querySelector('.source-tab.active')?.dataset.source
      || document.querySelector('#report-source')?.value
      || new URLSearchParams(location.search).get('source') || '';
    return ['caddo', 'batonrouge', 'lafayette', 'neworleans', 'lakecharles'].includes(source) ? source : 'all';
  }

  function getActionType(target, link, mapElement) {
    if (link) return 'navigation';
    if (mapElement) return 'map';
    if (target.matches?.('button, [role="button"]')) return 'button';
    if (target.matches?.('input, select, textarea')) return 'control';
    if (target.closest?.('.incident-card')) return 'incident_card';
    return 'content';
  }

  function getElementName(target, link = null, mapElement = null) {
    if (mapElement) {
      if (target.closest?.('.leaflet-marker-icon, .leaflet-interactive')) return 'map_marker';
      if (target.closest?.('.leaflet-control')) return 'map_control';
      return 'map_canvas';
    }
    if (target.closest?.('.incident-card')) return 'incident_card';

    const datasetKeys = ['analyticsLabel', 'viewMode', 'tab', 'infoTab', 'source', 'filter', 'urgency', 'brAgency', 'lafUnit', 'sort', 'action', 'month', 'date'];
    for (const key of datasetKeys) {
      if (target.dataset?.[key]) return cleanToken(`${key}_${target.dataset[key]}`, 80);
    }

    if (target.id) return cleanToken(target.id, 80);
    if (target.getAttribute?.('name')) return cleanToken(target.getAttribute('name'), 80);
    if (link) {
      const destination = safeDestination(link.href);
      if (destination) return cleanToken(destination, 80);
    }
    if (target.closest?.('.incident-card')) return 'incident_card';

    const accessibleName = target.getAttribute?.('aria-label') || target.getAttribute?.('title');
    if (accessibleName) return cleanToken(accessibleName, 80);

    const text = target.matches?.('button, a, [role="button"]') ? target.textContent : '';
    if (text?.trim()) return cleanToken(text, 80);

    const stableClasses = Array.from(target.classList || [])
      .filter((name) => !['active', 'hidden', 'open', 'selected', 'disabled'].includes(name))
      .slice(0, 2)
      .join('_');
    return cleanToken(stableClasses || target.tagName?.toLowerCase() || 'unknown', 80);
  }

  function getPageRegion(target, mapElement = null) {
    if (mapElement) return 'map';
    if (target.closest?.('[role="dialog"], .modal')) return 'dialog';
    if (target.closest?.('#incident-panel, #incident-list, .incident-list')) return 'incident_list';
    const namedRegion = target.closest?.('[data-analytics-region]')?.dataset.analyticsRegion;
    if (namedRegion) return cleanToken(namedRegion, 50);
    const region = target.closest?.('header, nav, aside, main, footer, section, article');
    return region?.tagName?.toLowerCase() || 'page';
  }

  function getSectionName(section) {
    const explicitName = section.dataset.analyticsSection || section.id;
    if (explicitName) return cleanToken(explicitName, 80);
    const heading = section.querySelector('h1, h2, h3');
    return cleanToken(heading?.textContent || '', 80);
  }

  function getClickPosition(event) {
    const x = Math.max(0, Math.min(99, Math.floor((event.clientX / Math.max(window.innerWidth, 1)) * 100)));
    const y = Math.max(0, Math.min(99, Math.floor((event.clientY / Math.max(window.innerHeight, 1)) * 100)));
    const pageY = event.clientY + window.scrollY;
    const page = Math.max(0, Math.min(99, Math.floor((pageY / Math.max(document.documentElement.scrollHeight, 1)) * 100)));
    return {
      xViewport: bucketPercent(x),
      yViewport: bucketPercent(y),
      yPage: bucketPercent(page)
    };
  }

  function safeDestination(href) {
    try {
      const url = new URL(href, location.href);
      if (url.protocol === 'mailto:' || url.protocol === 'tel:') return url.protocol.slice(0, -1);
      const pathname = url.pathname.replace(/\/incident\/[^/]+/g, '/incident/shared');
      return cleanToken(`${url.hostname === location.hostname ? '' : url.hostname}${pathname}`, 100);
    } catch (_) {
      return '';
    }
  }

  function safePageUrl(value, keepAttribution = false) {
    if (!value) return '';
    try {
      const url = new URL(value, location.href);
      const attribution = new URLSearchParams();
      if (keepAttribution) {
        // Retain standard campaign/ad attribution while dropping arbitrary input.
        for (const key of ['utm_source', 'utm_medium', 'utm_campaign', 'utm_id', 'utm_term', 'utm_content', 'gclid', 'dclid', 'gbraid', 'wbraid']) {
          if (url.searchParams.has(key)) attribution.set(key, url.searchParams.get(key));
        }
      }
      const query = attribution.toString();
      return `${url.origin}${url.pathname.replace(/\/incident\/[^/]+/g, '/incident/shared')}${query ? `?${query}` : ''}`;
    } catch (_) {
      return '';
    }
  }

  function safeLinkDomain(href) {
    try {
      const url = new URL(href, location.href);
      return ['http:', 'https:'].includes(url.protocol) ? cleanToken(url.hostname, 80) : url.protocol.slice(0, -1);
    } catch (_) {
      return '';
    }
  }

  function isOutboundLink(href) {
    try {
      const url = new URL(href, location.href);
      return ['http:', 'https:'].includes(url.protocol) && url.hostname !== location.hostname;
    } catch (_) {
      return false;
    }
  }

  function safeFileName(value) {
    if (!value) return '';
    try {
      return cleanToken(new URL(value, location.href).pathname.split('/').pop() || '', 80);
    } catch (_) {
      return '';
    }
  }

  function cleanToken(value, maxLength) {
    return String(value || '')
      .replace(/[\r\n\t]+/g, ' ')
      .replace(/\s+/g, ' ')
      .trim()
      .slice(0, maxLength);
  }

  function bucketPercent(percent) {
    const start = Math.floor(percent / 10) * 10;
    return `${start}-${Math.min(start + 9, 100)}`;
  }

  function bucketPixels(pixels) {
    if (pixels < 1000) return 'under_1000';
    if (pixels < 2500) return '1000_2499';
    if (pixels < 5000) return '2500_4999';
    return '5000_plus';
  }

  function roundedDuration(value) {
    return Math.max(0, Math.round(Number(value) || 0));
  }

  function rateLcp(value) {
    return value <= 2500 ? 'good' : value <= 4000 ? 'needs_improvement' : 'poor';
  }

  function rateCls(value) {
    return value <= 0.1 ? 'good' : value <= 0.25 ? 'needs_improvement' : 'poor';
  }

  function rateInp(value) {
    return value <= 200 ? 'good' : value <= 500 ? 'needs_improvement' : 'poor';
  }

  function scheduleIdle(callback) {
    if ('requestIdleCallback' in window) {
      window.requestIdleCallback(callback, { timeout: 2000 });
    } else {
      setTimeout(callback, 0);
    }
  }
})();
