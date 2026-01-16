import { getRuns } from "../../app/actions/viewer";
import { ViewerContainer } from "../../components/viewer/ViewerContainer";

// Force dynamic since we read from filesystem
export const dynamic = 'force-dynamic';

export default async function ViewerPage() {
    const runs = await getRuns();
    return <ViewerContainer initialRuns={runs} />;
}
