"use client";

import { useTheme } from "./ThemeProvider";
import { Moon, Sun } from "lucide-react";
import { Button } from "@digdir/designsystemet-react";
import styles from "../app/layout.module.css";

export default function Header() {
    const { theme, toggleTheme } = useTheme();

    return (
        <header style={{ padding: "1rem 2rem", display: "flex", justifyContent: "space-between", alignItems: "center", borderBottom: "1px solid rgba(0,0,0,0.05)", backgroundColor: "white" }}>
            <h2 style={{ fontSize: "1.25rem", fontWeight: 600, margin: 0 }}>Dashboard</h2>
            <Button
                variant="tertiary"
                color="neutral"
                onClick={toggleTheme}
                aria-label="Toggle theme"
            >
                {theme === "light" ? <Moon size={20} /> : <Sun size={20} />}
            </Button>
        </header>
    );
}
