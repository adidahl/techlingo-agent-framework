import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "@digdir/designsystemet-theme";
import "@digdir/designsystemet-css";
import "./globals.css";
import { ThemeProvider } from "../components/ThemeProvider";
import Sidebar from "../components/Sidebar";
import Header from "../components/Header";
import styles from "./layout.module.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "TechLingo Admin",
  description: "TechLingo Agent Framework Interface",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className={`${geistSans.variable} ${geistMono.variable}`}>
        <ThemeProvider>
          <div className={styles.layoutContainer}>
            <Sidebar />
            <div className={styles.mainWrapper}>
              <Header />
              <main className={styles.content}>{children}</main>
            </div>
          </div>
        </ThemeProvider>
      </body>
    </html>
  );
}
