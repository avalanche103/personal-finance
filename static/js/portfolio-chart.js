(function () {
    function chartColors() {
        const dark = document.documentElement.getAttribute('data-theme') === 'dark';
        return {
            line: '#0066CC',
            fill: dark ? 'rgba(0, 102, 204, 0.22)' : 'rgba(0, 102, 204, 0.08)',
            text: dark ? '#E0E0E0' : '#1A1A1A',
            grid: dark ? '#2d2d2d' : '#e5e7eb',
            paper: 'transparent',
        };
    }

    function renderPortfolioChart() {
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
        const trace = {
            x: payload.dates,
            y: payload.values,
            type: 'scatter',
            mode: 'lines',
            name: 'Portfolio USD',
            line: {color: colors.line, width: 2, shape: 'spline'},
            fill: 'tozeroy',
            fillcolor: colors.fill,
            hovertemplate: '%{x}<br>$%{y:,.2f}<extra></extra>',
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
                tickformat: '$,.0f',
                gridcolor: colors.grid,
                zeroline: false,
                tickfont: {family: 'IBM Plex Mono, monospace', size: 10},
            },
            hovermode: 'x unified',
            showlegend: false,
        };

        Plotly.react(target, [trace], layout, {
            responsive: true,
            displayModeBar: false,
        });
    }

    document.addEventListener('DOMContentLoaded', renderPortfolioChart);
    document.addEventListener('click', function (event) {
        if (event.target.closest('[data-theme-toggle]')) {
            window.setTimeout(renderPortfolioChart, 0);
        }
    });
})();
