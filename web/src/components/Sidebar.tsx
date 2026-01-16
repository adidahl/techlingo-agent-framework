"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Home, Zap, Settings, Box } from "lucide-react";

export default function Sidebar() {
    const pathname = usePathname();

    return (
        <aside style={{
            backgroundColor: "var(--color-bg-sidebar)",
            color: "var(--color-text-sidebar)",
            padding: "2rem 1.5rem",
            display: "flex",
            flexDirection: "column",
            height: "100%",
            gap: "2rem"
        }}>
            {/* Brand / Logo */}
            <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", paddingLeft: "0.5rem" }}>
                <div style={{ color: "var(--color-accent-lime)" }}>
                    <Zap size={28} fill="currentColor" />
                </div>
                <span style={{ fontSize: "1.25rem", fontWeight: "700", letterSpacing: "-0.02em" }}>
                    TechLingo Admin
                </span>
            </div>

            {/* Menu Section */}
            <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
                <div style={{ fontSize: "0.75rem", color: "rgba(255,255,255,0.4)", padding: "0 0.5rem", marginBottom: "0.5rem", letterSpacing: "0.05em" }}>
                    MENU
                </div>

                <Link href="/" className={`sidebar-link ${pathname === "/" ? "active" : ""}`}>
                    <Home size={20} />
                    Overview
                </Link>

                {/* Using Generator as "Statistics" or "Product" equivalent for now, but keeping name clear */}
                <Link href="/generator" className={`sidebar-link ${pathname === "/generator" ? "active" : ""}`}>
                    <Box size={20} />
                    Generator
                </Link>
            </div>

            <div style={{ marginTop: "auto" }}>
                <div style={{ fontSize: "0.75rem", color: "rgba(255,255,255,0.4)", padding: "0 0.5rem", marginBottom: "0.5rem", letterSpacing: "0.05em" }}>
                    GENERAL
                </div>
                <div className="sidebar-link" style={{ pointerEvents: 'none', opacity: 0.5 }}>
                    <Settings size={20} />
                    Settings
                </div>
            </div>
        </aside>
    );
}
