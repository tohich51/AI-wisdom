(function (window) {
  'use strict';

  const api = window.WisdomGraphCanvas || (window.WisdomGraphCanvas = {});
  // Ленивая загрузка библиотеки графа: cytoscape весит больше всех остальных
  // скриптов вместе взятых и нужен только на экране «Карта знаний» и только
  // когда в карте есть узлы. Файл в vendor не меняется, он подключается один раз.
  const libraryUrl = '/static/vendor/cytoscape-3.34.3.min.js';
  let loading = null;
  api.ensureLibrary = function ensureLibrary() {
    if (window.cytoscape) return Promise.resolve(true);
    if (loading) return loading;
    loading = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = libraryUrl;
      script.async = true;
      script.addEventListener('load', () => resolve(Boolean(window.cytoscape)));
      script.addEventListener('error', () => {
        loading = null;
        reject(new Error('Библиотека графа недоступна.'));
      });
      document.head.append(script);
    });
    return loading;
  };
  const colors = {
    source: '#2563eb', fragment: '#6b7280', knowledge: '#16a34a',
    rule: '#d97706', use: '#7c3aed', outcome: '#1e3a8a',
    concept: '#0f766e', default: '#475569'
  };

  api.render = function render(container, data, mode, onSelect, options = {}) {
    if (!window.cytoscape) {
      throw new Error('Библиотека графа недоступна.');
    }
    if (container && container.style) container.style.backgroundColor = '#ffffff';

    const rows = Array.isArray(data && data.nodes) ? data.nodes : [];
    const overview = rows.length > 40;
    const actualNodes = new Map();
    const elements = rows.map((node) => {
      actualNodes.set(String(node.id), node);
      const label = String(node.label == null ? '' : node.label).slice(0, 60);
      return { data: { id: node.id, label: label, kind: node.kind } };
    });
    const endpoints = new Set(actualNodes.keys());

    const edges = Array.isArray(data && data.edges) ? data.edges : [];
    edges.forEach((edge) => {
      const source = String(edge.source);
      const target = String(edge.target);
      if (source !== target && endpoints.has(source) && endpoints.has(target)) {
        elements.push({
          data: { id: edge.id, source: edge.source, target: edge.target,
            label: ({contains:'содержит',grounds:'подтверждает',retrieved:'получено',used:'применено',reports:'результат',supports:'поддерживает',part_of:'часть',used_for:'используется для',contradicts:'противоречит'})[edge.predicate] || String(edge.predicate || '') },
          classes: edge.hypothesis ? 'hypothesis' : ''
        });
      }
    });

    if (mode === 'semantic' && options.showSimilarity !== false &&
        data && data.similarity && data.similarity.available) {
      const similarityEdges = Array.isArray(data.similarity.edges)
        ? data.similarity.edges : [];
      similarityEdges.forEach((edge) => {
        const source = String(edge.source);
        const target = String(edge.target);
        const score = edge.score;
        if (source !== target && endpoints.has(source) && endpoints.has(target) &&
            Number.isFinite(score) && score >= -1 && score <= 1) {
          elements.push({
            data: {
              id: 'similarity:' + String(edge.id),
              source: edge.source,
              target: edge.target,
              label: 'cos ' + score.toFixed(2)
            },
            classes: 'similarity'
          });
        }
      });
    }

    const graph = window.cytoscape({
      container: container,
      elements: elements,
      minZoom: 0.04,
      maxZoom: 3,
      wheelSensitivity: 0.15,
      layout: { name: mode === 'semantic' ? 'circle' : 'cose',
        fit: true, padding: 30, animate: false, componentSpacing: 80,
        idealEdgeLength: 90, nodeRepulsion: 400000, nodeOverlap: 20 },
      style: [
        { selector: 'node', style: {
          shape: 'ellipse', width: 30, height: 30,
          'background-color': node => colors[node.data('kind')] || colors.default,
          label: node => overview && node.data('kind') !== 'source' ? '' : node.data('label'),
          color: '#111827', 'font-size': 11,
          'font-family': 'Arial, sans-serif', 'text-wrap': 'wrap', 'text-max-width': '90px',
          'text-outline-color': '#ffffff', 'text-outline-width': 2,
          'text-valign': 'center', 'text-halign': 'center'
        } },
        { selector: 'edge', style: {
          'curve-style': 'straight', 'target-arrow-shape': 'triangle', 'arrow-scale': 0.8,
          'line-color': '#64748b', 'target-arrow-color': '#64748b', width: 1.25,
          label: overview ? '' : 'data(label)', 'font-size': 11, 'text-rotation': 'autorotate',
          'text-background-color': '#ffffff', 'text-background-opacity': 0.9,
          'text-background-padding': 1, 'text-outline-color': '#ffffff', 'text-outline-width': 2
        } },
        { selector: 'edge.hypothesis', style: { 'line-style': 'dashed' } },
        { selector: 'edge.similarity', style: {
          'line-style': 'dashed', 'line-color': '#0f766e',
          'target-arrow-shape': 'none', label: 'data(label)'
        } },
        { selector: 'node:selected', style: { 'border-color': '#111827', 'border-width': 3 } }
      ]
    });

    graph.fit(30);

    let destroyed = false;
    let resizeFrame = null;
    let resizeObserver = null;
    let lastWidth = 0;
    let lastHeight = 0;

    const measureContainer = () => {
      const width = container.clientWidth;
      const height = container.clientHeight;
      const style = window.getComputedStyle ? window.getComputedStyle(container) : null;
      const hidden = container.isConnected === false || container.hidden === true ||
        (style && (style.display === 'none' ||
          style.visibility === 'hidden' || style.visibility === 'collapse')) ||
        width <= 0 || height <= 0;
      return {
        width: hidden ? 0 : width,
        height: hidden ? 0 : height
      };
    };

    const initialSize = measureContainer();
    lastWidth = initialSize.width;
    lastHeight = initialSize.height;

    const scheduleFit = () => {
      if (destroyed || resizeFrame !== null) return;
      resizeFrame = window.requestAnimationFrame(() => {
        resizeFrame = null;
        if (destroyed) return;
        const size = measureContainer();
        lastWidth = size.width;
        lastHeight = size.height;
        if (size.width <= 0 || size.height <= 0) return;
        graph.resize();
        graph.fit(30);
      });
    };

    const handleResize = () => {
      if (destroyed) return;
      const size = measureContainer();
      if (size.width === lastWidth && size.height === lastHeight) return;
      lastWidth = size.width;
      lastHeight = size.height;
      if (size.width <= 0 || size.height <= 0) {
        if (resizeFrame !== null) {
          window.cancelAnimationFrame(resizeFrame);
          resizeFrame = null;
        }
        return;
      }
      scheduleFit();
    };

    const cleanupResizeLifecycle = () => {
      if (destroyed) return;
      destroyed = true;
      if (resizeFrame !== null) {
        window.cancelAnimationFrame(resizeFrame);
        resizeFrame = null;
      }
      if (resizeObserver) resizeObserver.disconnect();
    };

    if (container && typeof window.ResizeObserver === 'function') {
      resizeObserver = new window.ResizeObserver(handleResize);
      resizeObserver.observe(container);
    }
    graph.on('destroy', cleanupResizeLifecycle);

    graph.on('tap', 'node', (event) => {
      const node = actualNodes.get(String(event.target.id()));
      if (node && typeof onSelect === 'function') onSelect(node);
    });
    return graph;
  };
}(window));
