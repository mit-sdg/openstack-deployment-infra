// Design system. See frontend/DESIGN.md for usage and copy rules.
export { Icon, type IconName } from "./Icon";
export { Mark } from "./Mark";
export { BoundaryText } from "./BoundaryText";
export {
  Button,
  IconButton,
  Spinner,
  buttonClass,
  type ButtonSize,
  type ButtonVariant,
} from "./components/Button";
export {
  Checkbox,
  Field,
  Fieldset,
  Hint,
  Input,
  PasswordInput,
  Radio,
  Select,
  Switch,
  Textarea,
} from "./components/Field";
export {
  AccountMenu,
  AppShell,
  Brand,
  menuItemClass,
  navLinkClass,
} from "./components/Shell";
export {
  AuthLayout,
  Card,
  Cluster,
  Grid,
  Page,
  PageHeader,
  Section,
  Stack,
  backLinkClass,
} from "./components/Page";
export {
  Alert,
  Badge,
  EmptyState,
  ErrorAlert,
  InlineStatus,
  LoadingRows,
  PageSkeleton,
  Skeleton,
  ToastProvider,
  useToast,
  type Tone,
} from "./components/Feedback";
export {
  CodeBlock,
  CopyField,
  DataTable,
  KeyValueList,
  List,
  ListItem,
  type Column,
} from "./components/Data";
export {
  Dialog,
  SegmentedControl,
  TabNav,
  tabClass,
} from "./components/Overlay";
export {
  ThemeToggle,
  useThemePreference,
  type ThemePreference,
} from "./components/ThemeToggle";

// Legacy primitives for the operator dashboard and owner-portal pages that
// have not been rebuilt yet. Do not use them in new code.
export { ThemeButton } from "./legacy/ThemeButton";
export { StatusBadge, type Tone as LegacyTone } from "./legacy/StatusBadge";
export { Card as LegacyCard, ActivityRow, ShellFrame } from "./legacy/Layout";
export { Empty, ErrorNotice, Loading } from "./legacy/Feedback";
