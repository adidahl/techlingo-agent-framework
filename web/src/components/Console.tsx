"use client";

import { useEffect, useRef } from "react";
import styles from "./Console.module.css";

interface ConsoleProps {
    logs: string[];
}

export default function Console({ logs }: ConsoleProps) {
    const scrollRef = useRef<HTMLDivElement>(null);

    useEffect(() => {
        if (scrollRef.current) {
            scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
        }
    }, [logs]);

    return (
        <div ref={scrollRef} className={styles.terminal}>
            {logs.map((log, i) => (
                <div key={i} className={styles.line}>
                    {log}
                </div>
            ))}
        </div>
    );
}
