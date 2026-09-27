import type { HeaderProps } from "@abbvie-unity/react";
import { H1, Logo, Header as UnityHeader } from "@abbvie-unity/react";
import { Link } from "@tanstack/react-router";

/**
 * App-level page header. Renders the logo/home link on the left and accepts children on the right for nav actions.
 * Customize `AppLogoLink` below to update the app name and branding.
 */
export function Header({ children, ...props }: HeaderProps) {
  return (
    // NOTE: If you want Header content to only grow to max width of 1440px and stay centered, remove `contentFullWidth`.
    <UnityHeader justify="space-between" contentFullWidth {...props}>
      <AppLogoLink />
      <div className="flex items-center gap-(--un-header-footer-space-gap)">
        {/* NOTE: 
              Business logic should stay out of UI components. 
              Ideally, refactor to pass components in as children of Header, or define props on Header to pass in data from outside this UI component */}
        {children}
      </div>
    </UnityHeader>
  );
}

const AppLogoLink = () => {
  return (
    <Link
      to="/"
      className="flex items-center gap-(--un-header-footer-space-gap) text-default no-underline visited:text-default"
    >
      <Logo kind="circle" />
      {/* The expansion lives on the Home and sign-in pages; the header stays compact, with
          the full name on hover for anyone who has not met the acronym yet. */}
      <H1
        styledAs="h2"
        className="text-nowrap"
        title="AI-Driven Report Intelligence & Automation"
      >
        ARIA
      </H1>
    </Link>
  );
};
