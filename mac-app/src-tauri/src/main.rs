#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::process::Command;
use std::time::Duration;

use tauri::{Manager, Url};

// Overridable at build time so cloners aren't hardcoded to this box:
// DRAWBOX_URL=https://yourbox.example.com npx tauri build
const DASHBOARD_URL: &str = match option_env!("DRAWBOX_URL") {
    Some(url) => url,
    None => "https://macole.draw-box.app/",
};
// The bundled offline/connecting page (frontendDist) is served here on macOS.
const LOCAL_URL: &str = "tauri://localhost";
const POLL_INTERVAL: Duration = Duration::from_secs(5);
// While the dashboard is open, require a few failed polls before yanking the
// user back to the offline screen, so a box reboot doesn't cause flapping.
const OFFLINE_STRIKES: u32 = 3;

/// HTTP status of the dashboard, or 0 on network failure.
fn dashboard_status() -> u32 {
    Command::new("curl")
        .args(["-s", "-o", "/dev/null", "-m", "4", "-w", "%{http_code}", DASHBOARD_URL])
        .output()
        .ok()
        .and_then(|out| String::from_utf8(out.stdout).ok())
        .and_then(|code| code.trim().parse().ok())
        .unwrap_or(0)
}

/// Any real status below 500 means the tunnel and box are alive — Cloudflare
/// Access redirects (302) and auth challenges (401) both count. When the
/// tunnel is down, Cloudflare's edge answers its "error 1033" page with HTTP
/// status 530, so a dead tunnel can never look online; 0 is a network failure.
fn is_online(status: u32) -> bool {
    status != 0 && status < 500
}

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            let window = app.get_webview_window("main").expect("main window");
            std::thread::spawn(move || {
                let dashboard = Url::parse(DASHBOARD_URL).unwrap();
                let local = Url::parse(LOCAL_URL).unwrap();
                let mut on_dashboard = false;
                let mut strikes = 0u32;
                loop {
                    if is_online(dashboard_status()) {
                        strikes = 0;
                        if !on_dashboard && window.navigate(dashboard.clone()).is_ok() {
                            on_dashboard = true;
                        }
                    } else if on_dashboard {
                        strikes += 1;
                        if strikes >= OFFLINE_STRIKES
                            && window.navigate(local.clone()).is_ok()
                        {
                            on_dashboard = false;
                            strikes = 0;
                        }
                    }
                    std::thread::sleep(POLL_INTERVAL);
                }
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running DrawBox");
}

#[cfg(test)]
mod tests {
    use super::is_online;

    #[test]
    fn healthy_statuses_are_online() {
        assert!(is_online(200));
        assert!(is_online(302)); // Cloudflare Access redirect
        assert!(is_online(401)); // auth challenge still proves the box is up
    }

    #[test]
    fn edge_errors_and_network_failures_are_offline() {
        assert!(!is_online(0)); // curl network failure
        assert!(!is_online(500));
        assert!(!is_online(530)); // Cloudflare error 1033: tunnel down
    }
}
