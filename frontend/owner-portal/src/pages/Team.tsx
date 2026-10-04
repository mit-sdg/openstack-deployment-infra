import { AppFrame } from '../components/AppFrame';
import { TeamSection } from '../components/TeamSection';

export function TeamPage({ id }: { id: string }) {
  return (
    <AppFrame id={id} active="Team">
      <TeamSection id={id} />
    </AppFrame>
  );
}
