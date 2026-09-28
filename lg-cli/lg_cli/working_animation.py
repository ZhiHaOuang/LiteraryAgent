"""Original Thinking Orbs motion with independent, playful status words."""
import random

# Curated language, never derived from private prompts or tool arguments.
WORDS = tuple('''simmering brewing steeping whisking kneading baking roasting toasting
caramelizing marinating seasoning stirring sprinkling folding proofing glazing
cooking tasting dreaming daydreaming stargazing moonwalking wandering rambling
meandering drifting floating sailing paddling surfing gliding soaring hovering
orbiting exploring discovering wondering imagining pondering musing unraveling
weaving knitting stitching threading embroidering sketching doodling scribbling
painting shading tracing sculpting carving shaping polishing tinkering assembling
arranging composing humming whistling singing dancing waltzing swaying bouncing
juggling balancing tiptoeing stretching unfurling blooming sprouting growing
rooting branching leafing ripening harvesting gathering collecting sorting sifting
puzzling decoding untangling connecting mapping charting navigating searching
reading rereading leafing listening noticing observing reflecting distilling
crystallizing sparkling glowing kindling flickering illuminating brightening
awakening conjuring enchanting storytelling'''.split())


def choose_word(previous=''):
    return random.choice([word for word in WORDS if word != previous])


def loading_text(elapsed, word, *, plain=False):
    if plain:
        return f'I am {word}…'
    from .thinking_orb import terminal_frame
    top, bottom = terminal_frame(max(0, int(elapsed * 8))).split('\n')
    return f'{top}  I am {word}…\n{bottom}'
