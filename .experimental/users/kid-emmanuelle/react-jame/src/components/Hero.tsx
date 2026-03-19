export default function Hero() {
  return (
    <div className="mb-10 text-center relative max-w-4xl mx-auto">
      <h1 className="text-7xl font-bold text-black leading-[0.9] mb-10 relative z-10">
        Agentic software<br />
        <span className="text-cyan-400">factory</span>
      </h1>
      <p className="text-lg text-gray-700 max-w-2xl mx-auto mb-10 relative z-10">
        Input your specs and sketches. A team of coding agents collaborates to design, code, and deploy your entire application.
      </p>
      <img src="/sparkle-green.svg" alt="" className="absolute -top-10 left-0 md:left-12 lg:left-10 w-20 h-20" />
      <img src="/sparkle-red.svg" alt="" className="absolute top-20 right-0 md:right-12 lg:right-14 w-20 h-20" />
    </div>
  );
}
