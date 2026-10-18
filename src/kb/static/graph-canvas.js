(function (window) {
  'use strict';

  const api = window.WisdomGraphCanvas || (window.WisdomGraphCanvas = {});
  const canonicalId = value => {
    if (typeof value === 'string' && value.trim()) return value;
    if (typeof value === 'number' && Number.isFinite(value)) return String(value);
    return '';
  };
  api.canonicalId = canonicalId;
  api.uniqueRowsById = (rows, keepMissing = false) => {
    const values = Array.isArray(rows) ? rows : [];
    const counts = new Map();
    for (const row of values) {
      const id = canonicalId(row && typeof row === 'object' ? row.id : undefined);
      if (id) counts.set(id, (counts.get(id) || 0) + 1);
    }
    return values.filter(row => {
      const id = canonicalId(row && typeof row === 'object' ? row.id : undefined);
      return id ? counts.get(id) === 1 : keepMissing === true;
    });
  };
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


  api.render = function render(container, data, mode, onSelect, options = {}) {
    if (!window.cytoscape) {
      throw new Error('Библиотека графа недоступна.');
    }
    const computed = container && window.getComputedStyle
      ? window.getComputedStyle(container) : null;
    const readPalette = () => {
      const probe = document.createElement('span');
      probe.style.cssText = 'position:fixed;width:0;height:0;visibility:hidden;pointer-events:none;forced-color-adjust:none';
      probe.style.color = 'var(--ink)';
      probe.style.backgroundColor = 'var(--surface)';
      probe.style.borderColor = 'var(--accent)';
      probe.style.textDecorationColor = 'var(--muted)';
      probe.style.outlineColor = 'var(--line-strong)';
      probe.style.caretColor = 'var(--focus)';
      probe.style.fontFamily = 'var(--graph-font-family)';
      probe.style.fontSize = 'var(--graph-label-size)';
      probe.style.width = 'var(--graph-label-max-width)';
      const host = container && container.isConnected ? container : document.body || document.documentElement;
      host.append(probe);
      const probeStyle = window.getComputedStyle(probe);
      const resolved = property => probeStyle.getPropertyValue(property).trim();
      const pixel = property => Number.parseFloat(
        window.getComputedStyle(probe).getPropertyValue(property)
      );
      const next = {
        ink: resolved('color'),
        surface: resolved('background-color'),
        accent: resolved('border-top-color'),
        muted: resolved('text-decoration-color'),
        lineStrong: resolved('outline-color'),
        focus: resolved('caret-color'),
        fontFamily: probeStyle.fontFamily.trim(),
        labelSize: Number.parseFloat(probeStyle.fontSize),
        labelMaxWidth: Number.parseFloat(resolved('width'))
      };
      probe.style.fontSize = 'var(--graph-overview-label-size)';
      next.overviewLabelSize = pixel('font-size');
      probe.style.fontSize = 'var(--graph-label-size)';
      probe.style.width = 'var(--graph-node-min-width)';
      next.nodeMinWidth = pixel('width');
      probe.style.width = 'var(--graph-node-padding)';
      next.nodePadding = pixel('width');
      probe.style.width = 'var(--graph-overview-node-size)';
      next.overviewNodeSize = pixel('width');
      probe.remove();
      return next;
    };
    let palette = readPalette();
    const fillsFor = theme => ({ source: theme.accent, fragment: theme.muted,
      knowledge: theme.ink, rule: theme.accent, use: theme.muted,
      outcome: theme.focus, concept: theme.accent, default: theme.ink });
    let fills = fillsFor(palette);
    if (container && container.style) container.style.backgroundColor = palette.surface;

    const graphId = canonicalId;
    const rows = Array.isArray(data && data.nodes) ? data.nodes : [];
    const nodeRows = api.uniqueRowsById(rows);
    const overview = nodeRows.length > 40;
    const actualNodes = new Map(nodeRows.map(node => [graphId(node.id), node]));
    const elementIds = new Set(nodeRows.map(node => graphId(node.id)));
    const labelProbe = document.createElement('span');
    labelProbe.style.cssText = 'position:fixed;left:-10000px;top:0;display:inline-block;visibility:hidden;pointer-events:none;white-space:normal;overflow-wrap:anywhere;box-sizing:border-box;line-height:normal';
    labelProbe.style.fontFamily = palette.fontFamily;
    labelProbe.style.fontSize = `${palette.labelSize}px`;
    const measurementHost = container && container.isConnected
      ? container : document.body || document.documentElement;
    measurementHost.append(labelProbe);
    const elements = nodeRows.map(node => {
      const id = graphId(node.id);
      const label = String(node.label == null ? '' : node.label).slice(0, 60);
      let width = palette.overviewNodeSize;
      let height = palette.overviewNodeSize;
      let textWidth = 1;
      if (!overview) {
        labelProbe.style.width = 'auto';
        labelProbe.textContent = label || '—';
        const naturalWidth = labelProbe.getBoundingClientRect().width;
        const innerWidth = Math.max(
          palette.nodeMinWidth - 2 * palette.nodePadding,
          Math.min(palette.labelMaxWidth, naturalWidth)
        );
        labelProbe.style.width = `${innerWidth}px`;
        const measured = labelProbe.getBoundingClientRect();
        width = Math.ceil(innerWidth + 2 * palette.nodePadding);
        height = Math.ceil(Math.max(
          measured.height + 2 * palette.nodePadding,
          palette.nodeMinWidth / 2
        ));
        textWidth = Math.max(1, Math.floor(innerWidth));
      }
      return { data: {
        id: id,
        label: label,
        kind: node.kind,
        width: width,
        height: height,
        textWidth: textWidth
      } };
    });
    labelProbe.remove();
    const endpoints = new Set(actualNodes.keys());

    const predicateLabels = {
      contains: 'содержит', grounds: 'подтверждает', retrieved: 'получено',
      used: 'применено', reports: 'результат', supports: 'поддерживает',
      part_of: 'часть', used_for: 'используется для', contradicts: 'противоречит'
    };
    const reserveElementId = (preferred, fallback) => {
      let id = preferred || fallback;
      if (elementIds.has(id)) id = fallback;
      let suffix = 0;
      while (elementIds.has(id)) {
        suffix += 1;
        id = fallback + '-' + suffix;
      }
      elementIds.add(id);
      return id;
    };
    const actualEdges = new Map();
    const edges = api.uniqueRowsById(
      Array.isArray(data && data.edges) ? data.edges : [], true
    );
    edges.forEach((edge, index) => {
      if (!edge || typeof edge !== 'object') return;
      const source = graphId(edge.source);
      const target = graphId(edge.target);
      if (source !== target && endpoints.has(source) && endpoints.has(target)) {
        const id = reserveElementId(graphId(edge.id), 'relation:' + index);
        actualEdges.set(id, edge);
        const predicate = typeof edge.predicate === 'string' &&
          Object.prototype.hasOwnProperty.call(predicateLabels, edge.predicate)
          ? predicateLabels[edge.predicate] : String(edge.predicate || '');
        elements.push({
          data: { id: id, source: source, target: target, label: predicate },
          classes: edge.hypothesis ? 'hypothesis' : ''
        });
      }
    });

    if (mode === 'semantic' && options.showSimilarity !== false &&
        data && data.similarity && data.similarity.available) {
      const similarityEdges = Array.isArray(data.similarity.edges)
        ? data.similarity.edges : [];
      similarityEdges.forEach((edge, index) => {
        const source = graphId(edge.source);
        const target = graphId(edge.target);
        const score = edge.score;
        if (source !== target && endpoints.has(source) && endpoints.has(target) &&
            Number.isFinite(score) && score >= -1 && score <= 1) {
          const rawId = graphId(edge.id);
          elements.push({
            data: {
              id: reserveElementId(rawId ? 'similarity:' + rawId : '', 'similarity:' + index),
              source: source,
              target: target,
              label: 'cos ' + score.toFixed(2)
            },
            classes: 'similarity'
          });
        }
      });
    }

    const legend = document.createElement('div');
    legend.className = 'graph-legend';
    legend.setAttribute('role', 'group');
    legend.setAttribute('aria-label', 'Обозначения графа');
    const legendTitle = document.createElement('strong');
    legendTitle.textContent = 'Обозначения графа';
    const legendList = document.createElement('ul');
    const legendItem = (markerClass, text) => {
      const item = document.createElement('li');
      const marker = document.createElement('span');
      marker.className = 'graph-legend-symbol ' + markerClass;
      marker.setAttribute('aria-hidden', 'true');
      const content = document.createElement('span');
      content.textContent = text;
      item.append(marker, content);
      legendList.append(item);
    };
    const kindNames = {
      source: 'Источник', fragment: 'Фрагмент', knowledge: 'Знание',
      rule: 'Правило', use: 'Применение', outcome: 'Результат', concept: 'Понятие'
    };
    const presentKinds = [...new Set(nodeRows.map(node =>
      node && typeof node.kind === 'string' ? node.kind : ''
    ).filter(Boolean))];
    for (const kind of presentKinds) {
      const kindName = Object.prototype.hasOwnProperty.call(kindNames, kind)
        ? kindNames[kind] : kind;
      legendItem('graph-legend-symbol--node', kindName + ': узел');
    }
    legendItem('graph-legend-symbol--typed',
      'Сплошная линия со стрелкой — направленная связь; подпись задаёт её тип.');
    legendItem('graph-legend-symbol--hypothesis',
      'Пунктирная направленная связь — гипотеза.');
    legendItem('graph-legend-symbol--similarity',
      'Пунктирная линия без стрелки — косинусное сходство понятий; это не направление и не доказательство.');
    legend.append(legendTitle, legendList);
    if (overview) {
      const note = document.createElement('p');
      note.className = 'graph-legend-note';
      note.textContent = 'Обзорный режим: подписи узлов скрыты, а полный список остаётся в DOM. Используйте список под графом; пересечения линий в плотной сети допустимы.';
      legend.append(note);
    }

    const graph = window.cytoscape({
      container: container,
      elements: elements,
      minZoom: 0.04,
      maxZoom: 3,
      wheelSensitivity: 0.15,
      layout: { name: overview || mode === 'semantic' ? 'circle' : 'cose',
        fit: true, padding: 30, animate: false, componentSpacing: 80,
        idealEdgeLength: 90, nodeRepulsion: 400000, nodeOverlap: 20,
        avoidOverlap: true, nodeDimensionsIncludeLabels: true },
      style: [
        { selector: 'node', style: {
          shape: 'round-rectangle',
          width: node => node.data('width'),
          height: node => node.data('height'),
          'background-color': node => fills[node.data('kind')] || fills.default,
          'background-opacity': 1,
          'border-color': () => palette.lineStrong,
          'border-width': 1,
          label: overview ? '' : 'data(label)',
          color: () => palette.ink,
          'font-size': () => palette.labelSize,
          'font-family': () => palette.fontFamily,
          'text-wrap': 'wrap',
          'text-max-width': node => `${Math.max(1, Number(node.data('textWidth')) || 1)}px`,
          'text-background-color': () => palette.surface,
          'text-background-opacity': 1,
          'text-background-padding': 2,
          'text-outline-color': () => palette.surface,
          'text-outline-width': 2,
          'text-valign': 'center', 'text-halign': 'center'
        } },
        { selector: 'edge', style: {
          'curve-style': 'bezier', 'target-arrow-shape': 'triangle', 'arrow-scale': 0.8,
          'line-color': () => palette.lineStrong, 'target-arrow-color': () => palette.lineStrong, width: 1.25,
          label: overview ? '' : 'data(label)',
          'font-size': () => overview ? palette.overviewLabelSize : palette.labelSize,
          'font-family': () => palette.fontFamily,
          'color': () => palette.ink,
          'text-rotation': 'autorotate',
          'text-background-color': () => palette.surface, 'text-background-opacity': 1,
          'text-background-padding': 2,
          'text-outline-color': () => palette.surface, 'text-outline-width': 2
        } },
        { selector: 'edge.hypothesis', style: { 'line-style': 'dashed' } },
        { selector: 'edge.similarity', style: {
          'line-style': 'dashed', 'line-color': () => palette.accent,
          'target-arrow-shape': 'none', label: 'data(label)'
        } },
        { selector: 'node:selected', style: { 'border-color': () => palette.focus, 'border-width': 3 } }
      ]
    });

    container.insertAdjacentElement('afterend', legend);
    graph.fit(30);

    let destroyed = false;
    let resizeFrame = null;
    let resizeObserver = null;
    let lastWidth = 0;
    let lastHeight = 0;
    let removeForcedColorsListener = null;
    const forcedColors = typeof window.matchMedia === 'function'
      ? window.matchMedia('(forced-colors: active)') : null;
    const repaintTheme = () => {
      if (destroyed) return;
      palette = readPalette();
      fills = fillsFor(palette);
      if (container && container.style) container.style.backgroundColor = palette.surface;
      graph.style().update();
      graph.resize();
    };
    if (forcedColors) {
      const handleForcedColorsChange = () => repaintTheme();
      if (typeof forcedColors.addEventListener === 'function' && typeof forcedColors.removeEventListener === 'function') {
        forcedColors.addEventListener('change', handleForcedColorsChange);
        removeForcedColorsListener = () => forcedColors.removeEventListener('change', handleForcedColorsChange);
      } else if (typeof forcedColors.addListener === 'function' && typeof forcedColors.removeListener === 'function') {
        forcedColors.addListener(handleForcedColorsChange);
        removeForcedColorsListener = () => forcedColors.removeListener(handleForcedColorsChange);
      }
    }

    const measureContainer = (knownStyle) => {
      const width = container.clientWidth;
      const height = container.clientHeight;
      const style = knownStyle || (window.getComputedStyle ? window.getComputedStyle(container) : null);
      const hidden = container.isConnected === false || container.hidden === true ||
        (style && (style.display === 'none' ||
          style.visibility === 'hidden' || style.visibility === 'collapse')) ||
        width <= 0 || height <= 0;
      return {
        width: hidden ? 0 : width,
        height: hidden ? 0 : height
      };
    };

    const initialSize = measureContainer(computed);
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
      if (resizeObserver) {
        resizeObserver.disconnect();
        resizeObserver = null;
      }
      if (removeForcedColorsListener) {
        removeForcedColorsListener();
        removeForcedColorsListener = null;
      }
    };

    if (container && typeof window.ResizeObserver === 'function') {
      resizeObserver = new window.ResizeObserver(handleResize);
      resizeObserver.observe(container);
    }
    graph.on('destroy', () => {
      legend.remove();
      cleanupResizeLifecycle();
    });

    graph.on('tap', 'node', (event) => {
      const node = actualNodes.get(graphId(event.target.id()));
      if (node && typeof onSelect === 'function') onSelect(node);
    });
    graph.on('tap', 'edge', event => {
      const edge = actualEdges.get(event.target.id());
      if (edge && typeof options.onSelectEdge === 'function') options.onSelectEdge(edge);
    });
    return graph;
  };
}(window));
