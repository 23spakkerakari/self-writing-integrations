import { Button as ButtonPrimitive } from "@base-ui/react/button";
import { cva, type VariantProps } from "class-variance-authority";
import { Database } from "lucide-react";
import { motion, MotionConfig } from "motion/react";
import { useId } from "react";
import { Link } from "react-router-dom";
import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/cn";

const buttonVariants = cva(
  "group/button inline-flex shrink-0 items-center justify-center rounded-sm border border-transparent bg-clip-padding text-sm font-medium whitespace-nowrap transition-all outline-none select-none focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink active:not-aria-[haspopup]:translate-y-px disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        default: "bg-ink text-white hover:bg-ink/85",
        outline: "border-line-strong bg-surface text-ink hover:bg-canvas aria-expanded:bg-canvas",
        secondary: "bg-canvas text-ink hover:bg-line aria-expanded:bg-canvas",
        ghost: "text-ink-2 hover:bg-canvas hover:text-ink aria-expanded:bg-canvas",
        destructive: "bg-bad-soft text-bad hover:bg-bad/20 focus-visible:outline-bad",
        link: "text-ink underline-offset-4 hover:underline",
      },
      size: {
        default: "h-8 gap-1.5 px-2.5",
        xs: "h-6 gap-1 px-2 text-xs [&_svg:not([class*='size-'])]:size-3",
        sm: "h-7 gap-1 px-2.5 text-[0.8rem] [&_svg:not([class*='size-'])]:size-3.5",
        lg: "h-9 gap-1.5 px-3",
        icon: "size-8",
        "icon-xs": "size-6 [&_svg:not([class*='size-'])]:size-3",
        "icon-sm": "size-7",
        "icon-lg": "size-9",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

function Button({
  className,
  variant = "default",
  size = "default",
  ...props
}: ButtonPrimitive.Props & VariantProps<typeof buttonVariants>) {
  return <ButtonPrimitive data-slot="button" className={cn(buttonVariants({ variant, size, className }))} {...props} />;
}

interface VisualContainerProps {
  children: React.ReactNode;
  className?: string;
}

interface IntegrationCardProps {
  visual: React.ReactNode;
  title: string;
  description: string;
  url: string;
  cta: string;
}

interface IntegrationItem {
  id: string;
  label: string;
  x: number;
  y: number;
  path: string;
  delay: number;
}

// The drawing is 564 by 410; the canonical object sits at 282, 205 and each source runs a line to it.
const integrations: IntegrationItem[] = [
  { id: "bamboohr", label: "BambooHR", x: 110, y: 90, path: "M 270 205 V 105 Q 270 90 255 90 H 110", delay: 0.1 },
  { id: "gusto", label: "Gusto", x: 360, y: 70, path: "M 294 205 V 85 Q 294 70 309 70 H 360", delay: 0.2 },
  { id: "workday", label: "Workday", x: 160, y: 205, path: "M 250 205 H 160", delay: 0.3 },
  { id: "rippling", label: "Rippling", x: 480, y: 205, path: "M 314 205 H 480", delay: 0.4 },
  { id: "personio", label: "Personio", x: 282, y: 360, path: "M 282 205 V 360", delay: 0.6 },
  { id: "internal", label: "Internal API", x: 460, y: 340, path: "M 314 215 V 325 Q 314 340 329 340 H 460", delay: 0.7 },
];

const AnimatedPath = ({ d, id }: { d: string; id: string }) => {
  return (
    <>
      <path d={d} stroke="currentColor" strokeWidth="1" fill="none" className="text-line-strong" />
      <motion.path
        d={d}
        stroke={`url(#${id})`}
        strokeWidth="2"
        fill="none"
        strokeDasharray="40 160"
        initial={{ strokeDashoffset: 200 }}
        animate={{ strokeDashoffset: -200 }}
        transition={{ duration: 4, repeat: Infinity, ease: "linear", delay: Math.random() * 2 }}
      />
      <defs>
        <linearGradient id={id} gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="transparent" />
          <stop offset="50%" stopColor="var(--color-ink)" stopOpacity="0.5" />
          <stop offset="100%" stopColor="transparent" />
        </linearGradient>
      </defs>
    </>
  );
};

/** Every source feeding the canonical Employee object. Lines carry a moving dash toward the centre. */
export function Integration() {
  const containerId = useId();

  return (
    <MotionConfig reducedMotion="user">
      <div className="relative h-full w-full">
        <svg
          className="pointer-events-none absolute inset-0 h-full w-full"
          viewBox="0 0 564 410"
          fill="none"
          xmlns="http://www.w3.org/2000/svg"
        >
          {integrations.map((integration) => (
            <AnimatedPath key={integration.id} d={integration.path} id={`${containerId}-${integration.id}`} />
          ))}
        </svg>

        <div className="absolute top-1/2 left-1/2 z-20 flex -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-sm border border-line bg-surface p-0.5 sm:p-1.5">
          <div className="flex flex-col items-center rounded-sm border border-line px-2.5 py-1.5 sm:px-3.5 sm:py-2.5">
            <Database className="size-5 text-ink" aria-hidden />
            <span className="mt-1 font-display text-sm leading-4 font-semibold">Employee</span>
            <span className="text-2xs text-ink-3">canonical</span>
          </div>
          <motion.div
            className="absolute inset-0 rounded-sm border-2 border-ink/10"
            animate={{ scale: [1, 1.15, 1], opacity: [0.3, 0, 0.3] }}
            transition={{ duration: 3, repeat: Infinity }}
          />
        </div>

        {integrations.map((integration) => (
          <motion.div
            key={integration.id}
            initial={{ opacity: 0, scale: 0.8 }}
            whileInView={{ opacity: 1, scale: 1 }}
            viewport={{ once: true }}
            transition={{ delay: integration.delay }}
            style={{ left: `${(integration.x / 564) * 100}%`, top: `${(integration.y / 410) * 100}%` }}
            className="absolute z-10 flex -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-sm border border-line bg-surface px-2 py-1 font-display text-xs font-medium whitespace-nowrap text-ink"
          >
            {integration.label}
          </motion.div>
        ))}
      </div>
    </MotionConfig>
  );
}

export function VisualContainer({ children, className }: VisualContainerProps) {
  return (
    <div
      className={cn(
        "relative flex aspect-564/460 w-full items-center justify-center overflow-hidden rounded-none bg-canvas p-8 sm:aspect-564/410",
        className,
      )}
    >
      <div
        className="absolute inset-0 opacity-20"
        style={{
          backgroundImage: "radial-gradient(circle, var(--color-ink) 1px, transparent 1px)",
          backgroundSize: "32px 32px",
        }}
      />
      <div className="relative z-10 flex h-full w-full items-center justify-center">{children}</div>
    </div>
  );
}

export function IntegrationCard({ visual, title, description, url, cta }: IntegrationCardProps) {
  return (
    <Card className="flex w-full flex-col gap-0 overflow-hidden rounded-sm p-0 sm:max-w-141">
      <VisualContainer>{visual}</VisualContainer>

      <CardContent className="flex flex-col gap-6 p-6 sm:gap-8 sm:p-8">
        <div className="flex flex-col gap-2">
          <h3 className="font-display text-xl font-semibold tracking-tight sm:text-2xl">{title}</h3>
          <p className="text-base leading-relaxed text-ink-2">{description}</p>
        </div>
        <Button nativeButton={false} className="h-10 w-fit px-5" render={<Link to={url} />}>
          {cta}
        </Button>
      </CardContent>
    </Card>
  );
}
