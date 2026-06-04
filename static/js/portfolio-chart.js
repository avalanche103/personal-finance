(function () {
    function chartColors() {
        const dark = document.documentElement.getAttribute('data-theme') === 'dark';
        return {
            line: '#0066CC',
            fill: dark ? 'rgba(0, 102, 204, 0.22)' : 'rgba(0, 102, 204, 0.08)',
            text: dark ? '#E0E0E0' : '#1A1A1A',
            grid: dark ? '#2d2d2d' : '#e5e7eb',
            paper: 'transparent',
            zero: dark ? '#666666' : '#9ca3af',
        };
    }

    function yAxisPadding(minValue, maxValue, includeZero) {
        let min = minValue;
        let max = maxValue;
        if (includeZero) {
            min = Math.min(min, 0);
            max = Math.max(max, 0);
        }
        const span = max - min;
        if (span === 0) {
            const cushion = Math.max(Math.abs(max) * 0.05, 0.5);
            return [min - cushion, max + cushion];
        }
        const padding = Math.max(span * 0.12, includeZero ? 0.05 : 0.5);
        return [min - padding, max + padding];
    }

    function drawPortfolioChart() {
        const payloadNode = document.getElementById('portfolio-chart-data');
        const target = document.getElementById('portfolio-chart');
        if (!payloadNode || !target || typeof Plotly === 'undefined') {
            return;
        }

        const payload = JSON.parse(payloadNode.textContent);
        if (!payload.values || payload.values.length < 2) {
            return;
        }

        const colors = chartColors();
        const isChangeMode = payload.mode === 'change';
        const yValues = isChangeMode ? payload.change_pct : payload.values;
        const yRange = yAxisPadding(
            Math.min(...yValues),
            Math.max(...yValues),
            isChangeMode,
        );

        const trace = {
            x: payload.dates,
            y: yValues,
            type: 'scatter',
            mode: 'lines+markers',
            name: isChangeMode ? 'Change %' : 'Portfolio USD',
            line: {color: colors.line, width: 2, shape: 'linear'},
            marker: {
                color: colors.line,
                size: payload.dates.length > 20 ? 4 : 6,
                line: {color: colors.paper, width: 1},
            },
            fill: 'none',
            customdata: payload.values.map((value, index) => ([
                value,
                payload.change_usd[index],
                payload.change_pct[index],
            ])),
            hovertemplate: isChangeMode
                ? '%{x}<br>%{y:+.2f}%<br>$%{customdata[0]:,.2f} (%{customdata[1]:+,.2f})<extra></extra>'
                : '%{x}<br>$%{y:,.2f}<br>%{customdata[2]:+.2f}% vs period start<extra></extra>',
        };

        const layout = {
            autosize: true,
            height: 220,
            margin: {l: 52, r: 16, t: 8, b: 32},
            paper_bgcolor: colors.paper,
            plot_bgcolor: colors.paper,
            font: {family: 'Inter, system-ui, sans-serif', size: 11, color: colors.text},
            xaxis: {
                showgrid: false,
                tickfont: {size: 10},
                tickangle: -35,
            },
            yaxis: {
                tickformat: isChangeMode ? '+.2f' : '$,.2f',
                ticksuffix: isChangeMode ? '%' : '',
                gridcolor: colors.grid,
                zeroline: isChangeMode,
                zerolinecolor: colors.zero,
                zerolinewidth: 1,
                range: yRange,
                fixedrange: false,
                tickfont: {family: 'IBM Plex Mono, monospace', size: 10},
            },
            hovermode: 'x unified',
            showlegend: false,
        };

        if (isChangeMode) {
            layout.shapes = [{
                type: 'line',
                xref: 'paper',
                x0: 0,
                x1: 1,
                yref: 'y',
                y0: 0,
                y1: 0,
                line: {color: colors.zero, width: 1, dash: 'dot'},
            }];
        }

        if (typeof Plotly.purge === 'function') {
            Plotly.purge(target);
        }
        Plotly.newPlot(target, [trace], layout, {
            responsive: true,
            displayModeBar: false,
        }).then(function () {
            return Plotly.Plots.resize(target);
        });
    }

    function schedulePortfolioChartRender() {
        window.requestAnimationFrame(function () {
            window.requestAnimationFrame(drawPortfolioChart);
        });
    }

    document.addEventListener('DOMContentLoaded', schedulePortfolioChartRender);
    document.body.addEventListener('htmx:afterSettle', function (event) {
        if (event.detail.target && event.detail.target.id === 'portfolio-chart-panel') {
            schedulePortfolioChartRender();
        }
    });
    document.addEventListener('click', function (event) {
        if (event.target.closest('[data-theme-toggle]')) {
            window.setTimeout(schedulePortfolioChartRender, 0);
        }
    });
})();
