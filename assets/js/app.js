document.querySelectorAll('a[href^="#"]').forEach((link) => {
    link.addEventListener("click", (event) => {
        const targetId = link.getAttribute("href");
        const target = document.querySelector(targetId);
        if (!target) {
            return;
        }
        event.preventDefault();
        target.scrollIntoView({ behavior: "smooth", block: "start" });
    });
});

const scrollTopButton = document.querySelector("[data-scroll-top]");
if (scrollTopButton) {
    const toggleScrollTopButton = () => {
        scrollTopButton.classList.toggle("is-visible", window.scrollY > 320);
    };

    scrollTopButton.addEventListener("click", () => {
        window.scrollTo({ top: 0, behavior: "smooth" });
    });

    window.addEventListener("scroll", toggleScrollTopButton, { passive: true });
    toggleScrollTopButton();
}

const dashboardLayout = document.querySelector("[data-dashboard-layout]");
if (dashboardLayout) {
    const sidebar = dashboardLayout.querySelector("[data-dashboard-sidebar]");
    const sidebarToggle = dashboardLayout.querySelector("[data-sidebar-toggle]");
    const sidebarClose = dashboardLayout.querySelector("[data-sidebar-close]");
    const backdrop = dashboardLayout.querySelector("[data-dashboard-backdrop]");
    const desktopQuery = window.matchMedia("(min-width: 1200px)");

    // Keep the navigation limited to real portal destinations. This also
    // removes stale View/Share links left by an older cached script.
    dashboardLayout.querySelectorAll(".sidebar-menu a, .sidebar-submenu a").forEach((link) => {
        if (["view", "share"].includes(link.textContent.trim().toLowerCase())) {
            link.remove();
        }
    });

    const isDrawerOpen = () => dashboardLayout.classList.contains("is-sidebar-open");

    const setDrawerState = (open) => {
        if (!sidebar || !sidebarToggle) {
            return;
        }

        dashboardLayout.classList.toggle("is-sidebar-open", open);
        sidebar.setAttribute("aria-hidden", String(!open));
        sidebarToggle.setAttribute("aria-expanded", String(open));
        sidebarToggle.setAttribute("aria-label", open ? "Close sidebar navigation" : "Open sidebar navigation");
        document.body.classList.toggle("dashboard-drawer-open", open);
    };

    const syncSidebarMode = () => {
        if (!sidebar || !sidebarToggle) {
            return;
        }

        if (desktopQuery.matches) {
            dashboardLayout.classList.remove("is-sidebar-open", "is-sidebar-collapsed");
            sidebarToggle.hidden = true;
            sidebar.removeAttribute("aria-hidden");
            sidebarToggle.setAttribute("aria-expanded", "true");
            sidebarToggle.setAttribute("aria-label", "Sidebar navigation is visible on desktop");
            document.body.classList.remove("dashboard-drawer-open");
            return;
        }

        sidebarToggle.hidden = false;
        setDrawerState(isDrawerOpen());
    };

    sidebarToggle?.addEventListener("click", () => {
        if (desktopQuery.matches) {
            return;
        }

        setDrawerState(!isDrawerOpen());
    });

    sidebarClose?.addEventListener("click", () => {
        if (desktopQuery.matches) {
            return;
        }

        setDrawerState(false);
    });

    backdrop?.addEventListener("click", () => {
        if (!desktopQuery.matches) {
            setDrawerState(false);
        }
    });

    dashboardLayout.querySelectorAll(".sidebar-menu a, .sidebar-submenu a").forEach((link) => {
        link.addEventListener("click", () => {
            if (!desktopQuery.matches) {
                setDrawerState(false);
            }
        });
    });

    const handleMediaChange = () => {
        syncSidebarMode();
    };

    if (typeof desktopQuery.addEventListener === "function") {
        desktopQuery.addEventListener("change", handleMediaChange);
    } else {
        desktopQuery.addListener(handleMediaChange);
    }

    window.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && !desktopQuery.matches && isDrawerOpen()) {
            setDrawerState(false);
        }
    });

    syncSidebarMode();
}

document.addEventListener("click", (event) => {
    const targetButton = event.target.closest("[data-toggle-target]");
    if (targetButton) {
        const selector = targetButton.getAttribute("data-toggle-target");
        const targetPanel = document.querySelector(selector);
        if (!targetPanel) {
            return;
        }
        const isHidden = targetPanel.hasAttribute("hidden");
        targetPanel.toggleAttribute("hidden");
        targetButton.setAttribute("aria-expanded", String(isHidden));
        if (isHidden) {
            const firstInput = targetPanel.querySelector("input, select, textarea");
            if (firstInput) {
                firstInput.focus();
            }
        }
        return;
    }

    const passwordToggle = event.target.closest("[data-password-toggle]");
    if (!passwordToggle) {
        return;
    }

    const passwordControl = passwordToggle.closest(".password-control");
    if (!passwordControl) {
        return;
    }

    const passwordInput = passwordControl.querySelector('input[type="password"], input[type="text"]');
    if (!passwordInput) {
        return;
    }

    const showingPassword = passwordInput.type === "text";
    passwordInput.type = showingPassword ? "password" : "text";
    passwordToggle.textContent = showingPassword ? "Show" : "Hide";
    passwordToggle.setAttribute("aria-pressed", String(!showingPassword));
});

// Keep department-dependent selects useful without making a user submit a
// filter once merely to see the programmes they can choose. Handbook forms
// intentionally have no programme control, so they are naturally excluded.
const programmeOptionsNode = document.getElementById("institution-programme-options");
const departmentOptionsNode = document.getElementById("institution-department-options");
if (programmeOptionsNode && departmentOptionsNode) {
    const programmeOptions = JSON.parse(programmeOptionsNode.textContent);
    const departmentOptions = JSON.parse(departmentOptionsNode.textContent);
    const fieldPrefix = (name) => name.includes("-") ? name.slice(0, name.lastIndexOf("-")) : "";

    document.querySelectorAll("form").forEach((form) => {
        const selects = Array.from(form.querySelectorAll("select"));
        const faculty = selects.find((item) => item.name.endsWith("faculty"));
        const department = selects.find((item) => item.name.endsWith("department"));
        const programme = selects.find((item) => item.name.endsWith("programme"));
        if (!department) return;

        const updateDepartments = () => {
            if (!faculty) return;
            const selected = new Set(Array.from(department.selectedOptions).map((item) => item.value));
            const facultyId = faculty.value;
            const options = facultyId
                ? departmentOptions.filter((item) => String(item.faculty_id) === facultyId)
                : departmentOptions;
            department.replaceChildren(new Option("All departments", ""));
            options.forEach((item) => department.add(new Option(
                `${item.name} (${item.code})`, item.id, selected.has(String(item.id)), selected.has(String(item.id)),
            )));
        };
        const updateProgrammes = () => {
            if (!programme) return;
            const selected = new Set(Array.from(programme.selectedOptions).map((item) => item.value));
            const departmentId = department.value;
            const options = departmentId
                ? programmeOptions.filter((item) => String(item.department_id) === departmentId)
                : [];
            programme.replaceChildren();
            if (!programme.multiple) programme.add(new Option("All programmes", ""));
            options.forEach((item) => programme.add(new Option(
                `${item.name} (${item.code})`, item.id, selected.has(String(item.id)), selected.has(String(item.id)),
            )));
            programme.disabled = !departmentId || !options.length;
        };
        faculty?.addEventListener("change", () => {
            updateDepartments();
            updateProgrammes();
        });
        department.addEventListener("change", updateProgrammes);
        if (faculty) updateDepartments();
        updateProgrammes();
    });
}

const getCsrfToken = () => {
    const csrfCookie = document.cookie
        .split(";")
        .map((item) => item.trim())
        .find((item) => item.startsWith("csrftoken="));
    return csrfCookie ? decodeURIComponent(csrfCookie.split("=")[1]) : "";
};

const departmentalFeesScript = document.querySelector("#departmental-fees-data");
const departmentalForm = document.querySelector("[data-departmental-form]");
if (departmentalFeesScript && departmentalForm) {
    const totalDisplay = departmentalForm.querySelector("[data-total-display]");
    const feeMap = JSON.parse(departmentalFeesScript.textContent);
    const formatCurrency = (value) => Number.parseFloat(value || 0).toFixed(2);

    const recalculateTotal = () => {
        let total = Number.parseFloat(feeMap.departmental_fee || 0);
        const checkedInputs = departmentalForm.querySelectorAll('input[type="radio"]:checked, input[type="checkbox"]:checked');
        checkedInputs.forEach((input) => {
            total += Number.parseFloat(feeMap[input.value] || 0);
        });
        if (totalDisplay) {
            totalDisplay.textContent = formatCurrency(total);
        }
    };

    departmentalForm.addEventListener("change", recalculateTotal);
    recalculateTotal();
}

const alertRuntime = document.querySelector(".alert-runtime");
if (alertRuntime && "Notification" in window) {
    const feedUrl = alertRuntime.dataset.alertFeedUrl;
    const preferenceUrl = alertRuntime.dataset.alertPreferencesUrl;
    const pushConfigurationUrl = alertRuntime.dataset.pushConfigurationUrl;
    const pushSubscriptionUrl = alertRuntime.dataset.pushSubscriptionUrl;
    const serviceWorkerUrl = alertRuntime.dataset.serviceWorkerUrl;
    const browserAlertsEnabled = alertRuntime.dataset.browserAlertsEnabled === "true";
    const classAlertsEnabled = alertRuntime.dataset.classAlertsEnabled === "true";
    const userRole = alertRuntime.dataset.userRole;
    const enableButton = document.querySelector("[data-enable-browser-alerts]");
    const alertPromptSubject = userRole === "student" ? "lecturer messages and class reminders" : "class reminders";

    const urlBase64ToUint8Array = (value) => {
        const paddedValue = `${value}${"=".repeat((4 - (value.length % 4)) % 4)}`
            .replace(/-/g, "+")
            .replace(/_/g, "/");
        const rawData = window.atob(paddedValue);
        return Uint8Array.from(rawData, (character) => character.charCodeAt(0));
    };

    // The legacy feed below covers an open dashboard. A service-worker
    // subscription is what lets an enrolled device receive alerts after the
    // page has been closed.
    const registerForBackgroundPush = async () => {
        if (!("serviceWorker" in navigator) || !("PushManager" in window) || !pushConfigurationUrl) {
            return false;
        }
        try {
            const configurationResponse = await fetch(pushConfigurationUrl, { credentials: "same-origin" });
            if (!configurationResponse.ok) {
                return false;
            }
            const configuration = await configurationResponse.json();
            if (!configuration.enabled || !configuration.public_key) {
                return false;
            }
            const registration = await navigator.serviceWorker.register(serviceWorkerUrl);
            const existingSubscription = await registration.pushManager.getSubscription();
            const subscription = existingSubscription || await registration.pushManager.subscribe({
                userVisibleOnly: true,
                applicationServerKey: urlBase64ToUint8Array(configuration.public_key),
            });
            const response = await fetch(pushSubscriptionUrl, {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": getCsrfToken(),
                },
                body: JSON.stringify(subscription),
            });
            return response.ok;
        } catch (error) {
            // Browser push is optional. The in-page alert feed remains active
            // and the user can still receive email reminders.
            return false;
        }
    };

    const syncAlertPreferences = async (browserEnabled, classEnabled) => {
        await fetch(preferenceUrl, {
            method: "POST",
            headers: {
                "Content-Type": "application/x-www-form-urlencoded",
                "X-CSRFToken": getCsrfToken(),
            },
            body: new URLSearchParams({
                browser_alerts_enabled: String(browserEnabled),
                class_reminder_alerts_enabled: String(classEnabled),
            }),
        });
    };

    const showBrowserAlert = (alertItem) => {
        const notification = new Notification(alertItem.title, {
            body: alertItem.body,
            tag: `educonnect-${alertItem.id}`,
        });
        notification.onclick = () => {
            if (alertItem.target_url) {
                window.location.href = alertItem.target_url;
            }
            notification.close();
        };
    };

    const pollAlerts = async () => {
        if (Notification.permission !== "granted") {
            return;
        }
        const response = await fetch(feedUrl, { credentials: "same-origin" });
        if (!response.ok) {
            return;
        }
        const payload = await response.json();
        (payload.alerts || []).forEach(showBrowserAlert);
    };

    if (browserAlertsEnabled && Notification.permission === "granted") {
        window.setInterval(pollAlerts, 30000);
        pollAlerts();
        registerForBackgroundPush();
    }

    if (enableButton) {
        enableButton.addEventListener("click", async () => {
            const permission = await Notification.requestPermission();
            if (permission !== "granted") {
                await syncAlertPreferences(false, false);
                enableButton.textContent = "Enable browser alerts";
                return;
            }
            const wantsClassAlerts = window.confirm("Do you want class reminders to appear as browser alerts 30 minutes before class?");
            await syncAlertPreferences(true, wantsClassAlerts);
            enableButton.textContent = "Browser alerts enabled";
            await registerForBackgroundPush();
            pollAlerts();
            window.setInterval(pollAlerts, 30000);
        });
    } else if (!browserAlertsEnabled && Notification.permission === "default") {
        window.setTimeout(async () => {
            const wantsAlerts = window.confirm(`Enable browser alerts on this device for ${alertPromptSubject}?`);
            if (!wantsAlerts) {
                return;
            }
            const permission = await Notification.requestPermission();
            if (permission !== "granted") {
                return;
            }
            const wantsClassAlerts = window.confirm("Do you want class reminders to appear as browser alerts 30 minutes before class?");
            await syncAlertPreferences(true, wantsClassAlerts);
            await registerForBackgroundPush();
            pollAlerts();
            window.setInterval(pollAlerts, 30000);
        }, 1200);
    }
}
