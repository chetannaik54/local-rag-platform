import "@testing-library/jest-dom/vitest";

// jsdom lacks these
Element.prototype.scrollIntoView = () => {};
