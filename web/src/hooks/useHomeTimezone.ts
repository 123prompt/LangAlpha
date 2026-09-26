import { currentTimezoneName, deviceTimezone, isKnownTimezone } from '@/lib/deviceTimezone';
import { useUser } from './useUser';

/** The zone the user set in Settings, or this device's until they set one
 *  (or when this browser cannot format in the one they set). */
export function useHomeTimezone(): string {
  const { user } = useUser();
  const tz = user?.timezone;
  return tz && isKnownTimezone(tz) ? currentTimezoneName(tz) : deviceTimezone();
}
