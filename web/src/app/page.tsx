import styles from "./page.module.css";
import { Button } from "@digdir/designsystemet-react";

export default function Home() {
  return (
    <div className={styles.page}>
      <main className={styles.main}>
        <h1>Designsystemet Integration</h1>
        <p>This is a test to verify the installation of Designsystemet.</p>
        <div style={{ marginTop: "20px" }}>
          <Button>Click me!</Button>
        </div>
      </main>
    </div>
  );
}
