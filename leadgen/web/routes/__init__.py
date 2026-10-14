from leadgen.web.routes import companies, dashboard, exports, leads, runs, settings_routes, titles, tools


def all_routers():
    return [dashboard.router, runs.router, leads.router, companies.router, titles.router, settings_routes.router,
            exports.router, tools.router]
